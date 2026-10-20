"""Break each guard in a copy of ai-hub and report which tests fall.

The original tree is never written: every mutation runs in a fresh copy.
Each fragment must appear exactly once, or the mutation is reported as not
applied. Run from anywhere:

    python -B tools/mutations.py
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
# No console window for the test runs on Windows.
NO_WINDOW = 0x08000000 if os.name == "nt" else 0

MUTATIONS = [
    ("index une requete active", "aihub/db.py",
     "    CREATE UNIQUE INDEX one_active_request ON messages(conversation_id)\n        WHERE status IN ('pending', 'running')\n",
     "    CREATE INDEX one_active_request ON messages(conversation_id)\n"),
    ("index un plan ouvert", "aihub/db.py",
     "    CREATE UNIQUE INDEX one_open_plan ON plans(conversation_id)",
     "    CREATE INDEX one_open_plan ON plans(conversation_id)"),
    ("CHECK bypass en base", "aihub/db.py",
     "        CHECK (permission_mode <> 'bypass' OR bypass_confirmed_at IS NOT NULL)\n",
     "        CHECK (1)\n"),
    ("secure_delete", "aihub/db.py",
     'conn.execute("PRAGMA secure_delete = ON")', 'conn.execute("PRAGMA secure_delete = OFF")'),
    ("checkpoint apres suppression", "aihub/store.py",
     '    conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))\n'
     '    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")\n',
     '    conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))\n'),
    ("checkpoint apres suppression d'un projet", "aihub/store.py",
     '        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))\n'
     '    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")\n',
     '        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))\n'),
    # The active trigger is the version 4 one (earlier ones are dropped by the migrations).
    ("exception SET NULL du gel", "aihub/db.py",
     "         AND NOT (NEW.agent_id IS NULL\n                  AND NEW.description IS OLD.description\n"
     "                  AND NEW.provider IS OLD.provider\n                  AND NEW.model IS OLD.model\n"
     "                  AND NEW.position IS OLD.position\n                  AND NEW.title IS OLD.title\n"
     "                  AND NEW.files_json IS OLD.files_json\n                  AND NEW.kind IS OLD.kind\n                  AND NEW.assignment_reason IS OLD.assignment_reason)\n         AND NOT ((SELECT status FROM plans WHERE id = OLD.plan_id) IN (\'validated\', \'running\', \'error\', \'stopped\')",
     "         AND NOT (0\n                  AND NEW.description IS OLD.description\n"
     "                  AND NEW.provider IS OLD.provider\n                  AND NEW.model IS OLD.model\n"
     "                  AND NEW.position IS OLD.position\n                  AND NEW.title IS OLD.title\n"
     "                  AND NEW.files_json IS OLD.files_json\n                  AND NEW.kind IS OLD.kind\n                  AND NEW.assignment_reason IS OLD.assignment_reason)\n         AND NOT ((SELECT status FROM plans WHERE id = OLD.plan_id) IN (\'validated\', \'running\', \'error\', \'stopped\')"),
    ("trigger gel insert", "aihub/db.py",
     "WHEN (SELECT status FROM plans WHERE id = NEW.plan_id) <> 'proposed'",
     "WHEN 0"),
    ("trigger meme plan", "aihub/db.py",
     "         IS NOT (SELECT plan_id FROM subtasks WHERE id = NEW.depends_on_id)",
     "         IS NOT (SELECT plan_id FROM subtasks WHERE id = NEW.subtask_id)"),
    ("store: bypass sans confirmation (set)", "aihub/store.py",
     '    get_conversation(conn, conversation_id)\n    if permission_mode == "bypass" and not confirm_bypass:\n',
     '    get_conversation(conn, conversation_id)\n    if False:\n'),
    ("store: oubli confirmation en quittant bypass", "aihub/store.py",
     '        (permission_mode, now if permission_mode == "bypass" else None, now, conversation_id),',
     '        (permission_mode, now, now, conversation_id),'),
    ("store: plan fige (assign)", "aihub/store.py",
     '    subtask = _get_subtask(conn, subtask_id)\n    _require_proposed(conn, subtask["plan_id"])\n    provider, model = _agent_snapshot(conn, agent_id, model)\n    with transaction(conn):\n        conn.execute(\n            "UPDATE subtasks SET agent_id',
     '    subtask = _get_subtask(conn, subtask_id)\n    provider, model = _agent_snapshot(conn, agent_id, model)\n    with transaction(conn):\n        conn.execute(\n            "UPDATE subtasks SET agent_id'),
    ("store: boucle de dependances", "aihub/store.py",
     "    if _would_cycle(conn, subtask_id, depends_on_id):", "    if False:"),
    ("store: dependances attendues (ready)", "aihub/store.py",
     "WHERE d.subtask_id = s.id AND dep.status <> 'ok'", "WHERE d.subtask_id = s.id AND 0"),
    ("store: agent desactive en validation", "aihub/store.py",
     "    if disabled:\n        raise", "    if False:\n        raise"),
    ("store: sous-tache sans agent en validation", "aihub/store.py",
     "    if unassigned:\n        raise", "    if False:\n        raise"),
    ("store: transitions de statut", "aihub/store.py",
     "    if new not in _TRANSITIONS.get(current, set()):", "    if False:"),
    ("store: suppression pendant une requete", "aihub/store.py",
     '    if active_request(conn, conversation_id) is not None:\n        raise Busy("arrêtez la requête',
     '    if False:\n        raise Busy("arrêtez la requête'),
    ("store: suppression agent occupe", "aihub/store.py",
     "    if busy:\n", "    if False:\n"),
    ("store: changement de mode avec plan ouvert", "aihub/store.py",
     '        if open_plan(conn, conversation_id) is not None:\n            raise Busy("un plan est ouvert',
     '        if False:\n            raise Busy("un plan est ouvert'),
    ("store: renumerotation", "aihub/store.py",
     '            "UPDATE subtasks SET position = position - ? WHERE plan_id = ?",',
     '            "UPDATE subtasks SET position = position - 0 * ? WHERE plan_id = ?",'),
    ("db: fermeture sur refus", "aihub/db.py",
     "        conn.close()\n        raise\n", "        raise\n"),
    ("store: mesure negative", "aihub/store.py",
     "or value < 0)", "or value < -10)"),
    # --- phase 2: Claude Code provider, runner and command line ---
    ("cc: refus sans personne pour valider", "aihub/providers/claude_code.py",
     'decision = Decision(False, "aucune personne n\'est là pour valider',
     'decision = Decision(True, "aucune personne n\'est là pour valider'),
    ("runner: refus sans personne pour valider", "aihub/runner.py",
     'decision = claude_code.Decision(False, "aucune personne',
     'decision = claude_code.Decision(True, "aucune personne'),
    ("cc: callback en panne approuve", "aihub/providers/claude_code.py",
     'decision = Decision(False, f"validation impossible', 'decision = Decision(True, f"validation impossible'),
    ("cc: reponse invalide approuve", "aihub/providers/claude_code.py",
     'decision = Decision(False, "réponse de validation invalide',
     'decision = Decision(True, "réponse de validation invalide'),
    ("cc: entree modifiee ignoree", "aihub/providers/claude_code.py",
     '"updatedInput": decision.updated_input if decision.updated_input is not None\n                        else ask.input}',
     '"updatedInput": ask.input}'),
    ("cc: arret sans les enfants", "aihub/providers/claude_code.py",
     '["taskkill", "/T", "/F",', '["taskkill", "/F",'),
    ("cc: arret compte comme succes", "aihub/providers/claude_code.py",
     'if reason in ("user", "plan_rejected"):\n            result.status = "stopped"',
     'if reason in ("user", "plan_rejected"):\n            result.status = "ok"'),
    ("cc: delai jamais arme", "aihub/providers/claude_code.py",
     "            timer.start()\n", "            pass\n"),
    ("cc: erreur de la CLI ignoree", "aihub/providers/claude_code.py",
     'if final.get("is_error"):', "if False:"),
    ("cc: quota sans nom d'IA", "aihub/providers/claude_code.py",
     'who = f"{DISPLAY_NAME} ({model})" if model else DISPLAY_NAME', 'who = "l\'IA"'),
    ("cc: tubes non fermes", "aihub/providers/claude_code.py",
     "                    stream.close()\n", "                    pass\n"),
    ("cc: MCP de l'utilisateur charges", "aihub/providers/claude_code.py",
     '            "--strict-mcp-config",\n', ""),
    ("cc: autorisations non renvoyees a l'hote", "aihub/providers/claude_code.py",
     '            "--permission-prompts", "host",\n', ""),
    ("cc: demandes non routees vers l'hote (stdio)", "aihub/providers/claude_code.py",
     '            "--permission-prompt-tool", "stdio",\n', ""),
    # 1.3.0: the copies of the Claude app are compared in aihub/locate.py.
    ("cc: versions comparees comme du texte", "aihub/locate.py",
     'return tuple(int(part) for part in text.split("."))', 'return tuple(text.split("."))'),
    ("runner: texte non enregistre en direct", "aihub/runner.py",
     "        store.append_message_content(self.conn, self.message_id, chunk)\n", ""),
    ("runner: autorisation non journalisee", "aihub/runner.py",
     "        if isinstance(decision, claude_code.Decision):\n            store.record_permission(",
     "        if False:\n            store.record_permission("),
    ("runner: session non reprise", "aihub/runner.py",
     "resume_id = last[1] if last and last[0] == self.provider else None", "resume_id = None"),
    ("runner: reponse laissee en cours apres un bogue", "aihub/runner.py",
     '            store.finish_message(self.conn, self.message_id, "stopped")\n            raise',
     "            raise"),
    ("runner: modele par defaut force", "aihub/runner.py",
     'model=providers.default_model(self.provider, agent["model"]),', 'model=agent["model"],'),
    ("runner: quota non enregistre", "aihub/runner.py",
     "        store.record_quota(\n", "        (lambda *a, **k: None)(\n"),
    ("cc: variables de la session hote heritees", "aihub/providers/claude_code.py",
     "env=self.env if self.env is not None else clean_env(os.environ),",
     "env=self.env if self.env is not None else dict(os.environ),"),
    ("cc: ANTHROPIC_BASE_URL de l'hote garde", "aihub/providers/claude_code.py",
     '_HOST_SESSION_NAMES = {"CLAUDECODE", "CLAUDE_PID", "ANTHROPIC_BASE_URL"}',
     '_HOST_SESSION_NAMES = {"CLAUDECODE", "CLAUDE_PID"}'),
    # --- phase 3: web server ---
    ("web: delai sans reponse vaut accord", "aihub/web/jobs.py",
     'self._decide(item, Decision(False, "pas de réponse dans le délai : refusé"))',
     'self._decide(item, Decision(True, "pas de réponse dans le délai : refusé"))'),
    ("web: arret laisse les demandes en attente", "aihub/web/jobs.py",
     '        for item in pending:\n            self._decide(item, Decision(False, "requête arrêtée"))\n',
     ""),
    ("web: question validee sans reponse", "aihub/web/jobs.py",
     "if not isinstance(answers, dict) or not answers:", "if False:"),
    ("web: reponses non transmises a l'agent", "aihub/web/jobs.py",
     'updated = {**item.input, "answers": {str(k): str(v) for k, v in answers.items()}}',
     "updated = None"),
    ("web: fin annoncee avant liberation", "aihub/web/jobs.py",
     "            with self._lock:\n                if self._jobs.get(cid) is job:\n                    del self._jobs[cid]\n"
     "            if conn is not None:\n                conn.close()\n            job.started.set()\n"
     "            if done is not None:\n                self.hub.publish(cid, {\"type\": \"done\", \"kind\": job.kind, **done})\n",
     "            if done is not None:\n                self.hub.publish(cid, {\"type\": \"done\", \"kind\": job.kind, **done})\n"
     "            with self._lock:\n                if self._jobs.get(cid) is job:\n                    del self._jobs[cid]\n"
     "            if conn is not None:\n                conn.close()\n            job.started.set()\n"),
    ("web: recuperation au demarrage absente", "aihub/web/jobs.py",
     "self.recovered = store.recover_interrupted(conn)", "self.recovered = 0"),
    ("web: deux taches pour une conversation", "aihub/web/jobs.py",
     '            if conversation_id in self._jobs:\n                raise store.Busy(',
     '            if False:\n                raise store.Busy('),
    ("web: decalage du texte faux", "aihub/web/jobs.py",
     "                offset = job.length\n", "                offset = 0\n"),
    ("web: dossiers autorises ignores", "aihub/places.py",
     "        if not self.allows(path):\n", "        if False:\n"),
    ("web: chemin relatif accepte", "aihub/places.py",
     '        if not path.is_absolute() or (os.name == "nt" and not path.drive):\n', "        if False:\n"),
    ("web: occupe renvoie 400", "aihub/web/app.py",
     '        return JSONResponse({"detail": str(exc)}, 409)\n', '        return JSONResponse({"detail": str(exc)}, 400)\n'),
    ("web: code du warning absent", "aihub/web/app.py",
     '"code": "bypass_confirmation_required"', '"code": None'),
    ("web: mode change sous un agent", "aihub/web/app.py",
     "                if jobs.running(cid):\n                    raise Busy(\"une requête est en cours : arrêtez-la avant de changer de mode\")",
     "                if False:\n                    raise Busy(\"une requête est en cours : arrêtez-la avant de changer de mode\")"),
    ("runner: dossier absent detecte trop tard", "aihub/runner.py",
     '        if not Path(conversation["workdir"]).is_dir():\n', "        if False:\n"),
    ("store: plans non clos a la recuperation", "aihub/store.py",
     "\"UPDATE plans SET status = 'stopped', finished_at = ? WHERE status = 'running'\"",
     "\"UPDATE plans SET status = 'stopped', finished_at = ? WHERE 0\""),
    # --- phase 4: orchestration ---
    ("orch: chef pas en lecture seule", "aihub/orchestrator.py",
     "resume=False, tools=READ_ONLY_TOOLS, json_schema=plan_schema(),",
     "resume=False, tools=None, json_schema=plan_schema(),"),
    ("orch: limite du chef ignoree", "aihub/orchestrator.py",
     "                if len(threads) >= limit:\n", "                if False:\n"),
    ("orch: fichiers declares communs ignores", "aihub/orchestrator.py",
     "if running_files and (ALL_FILES in files or ALL_FILES in busy or files & busy):",
     "if False:"),
    ("orch: tout le projet pas exclusif", "aihub/orchestrator.py",
     "if running_files and (ALL_FILES in files or ALL_FILES in busy or files & busy):",
     "if running_files and (files & busy):"),
    ("orch: verrou d'ecriture ignore", "aihub/orchestrator.py",
     "while not self._stopping and any(", "while False and any("),
    ("orch: fichier libere avant la fin annoncee", "aihub/orchestrator.py",
     "            self._record(conn, sid, result, error)\n            self._release(sid)\n",
     "            self._release(sid)\n            self._record(conn, sid, result, error)\n"),
    ("orch: refus garde le fichier", "aihub/orchestrator.py",
     "        if claimed_here and not decision.allow:\n", "        if False:\n"),
    ("orch: resultats des dependances perdus", "aihub/orchestrator.py",
     '                deps = [by_id[d] for d in subtask["depends_on"]]\n', "                deps = []\n"),
    ("orch: pas de synthese", "aihub/orchestrator.py",
     "        self._synthesize(conn, conversation, request_text)\n", ""),
    ("orch: arret laisse tourner les agents", "aihub/orchestrator.py",
     "        for run in runs:\n            run.stop()\n", ""),
    ("orch: echec presente comme reussite", "aihub/orchestrator.py",
     '"error": "ÉCHEC"', '"error": "réussie"'),
    ("orch: parallelisme du chef non plafonne", "aihub/orchestrator.py",
     "min(parallel, MAX_PARALLEL, len(specs))", "parallel"),
    ("orch: modele de la sous-tache ignore", "aihub/orchestrator.py",
     'model=providers.default_model(provider, subtask["model"]),', "model=None,"),
    ("runner: plan mal forme accepte", "aihub/runner.py",
     '                result.status, result.error = "error", refusal\n', "                pass\n"),
    # --- discussion with the conductor ---
    ("orch: second plan refuse apres le chef", "aihub/orchestrator.py",
     "    if store.open_plan(conn, conversation_id) is not None:\n        # Checked before the conductor runs",
     "    if False:\n        # Checked before the conductor runs"),
    ("orch: discussion pas en lecture seule", "aihub/orchestrator.py",
     "record_prompt=False, resume=True, tools=READ_ONLY_TOOLS, retrieval_prompt=text, **request_kwargs)",
     "record_prompt=False, resume=True, tools=None, retrieval_prompt=text, **request_kwargs)"),
    ("orch: discussion sans reprise de session", "aihub/orchestrator.py",
     "record_prompt=False, resume=True, tools=READ_ONLY_TOOLS, retrieval_prompt=text, **request_kwargs)",
     "record_prompt=False, resume=False, tools=READ_ONLY_TOOLS, retrieval_prompt=text, **request_kwargs)"),
    ("orch: discussion sans le plan ouvert", "aihub/orchestrator.py",
     'plan = store.get_plan(conn, row["id"]) if row is not None and row["status"] == "proposed" else None',
     "plan = None"),
    ("orch: adaptation sans la discussion", "aihub/orchestrator.py",
     "discussion_since(conn, plan)),", "()),"),
    ("orch: reponses de plan prises pour la discussion", "aihub/orchestrator.py",
     '            if not (r["role"] == "assistant" and (r["content"] or "").startswith(("Plan proposé", "Plan adapté")))]',
     "            if True]"),
    ("web: discussion envoyee au planificateur", "aihub/web/app.py",
     '            if body.kind == "discuss":\n', "            if False:\n"),
    ("db: resume et parallelisme modifiables apres validation", "aihub/db.py",
     "    BEFORE UPDATE OF summary, max_parallel ON plans\n    WHEN OLD.status <> 'proposed'\n",
     "    BEFORE UPDATE OF summary, max_parallel ON plans\n    WHEN 0\n"),
    ("web: plan modifiable pendant que le chef travaille", "aihub/web/app.py",
     '        if jobs.running(plan["conversation_id"]):\n            raise Busy("le chef',
     '        if False:\n            raise Busy("le chef'),
    ("web: orchestration envoyee a un seul agent", "aihub/web/app.py",
     'if conv["mode"] == "orchestration":\n            if body.kind == "discuss":',
     'if False:\n            if body.kind == "discuss":'),
    # --- phase 5: automatic assignment ---
    ("route: IA non branchee choisie", "aihub/routing.py",
     "        if rule.provider not in connected:\n", "        if False:\n"),
    ("route: agents retires ignores", "aihub/routing.py",
     "        if (rule.provider, rule.model) in excluded:\n", "        if False:\n"),
    ("route: raison sans le meilleur choix", "aihub/routing.py",
     "        if unusable:\n            best, why = unusable[0]\n", "        if False:\n            best, why = unusable[0]\n"),
    ("route: mode auto ignore au plan", "aihub/orchestrator.py",
     "        routing.auto_assign(conn, plan_id, connected=connected)\n", "        pass\n"),
    ("route: trous non combles apres adaptation", "aihub/orchestrator.py",
     "        routing.auto_assign(conn, plan_id, only_unassigned=True, connected=connected)\n", "        pass\n"),
    ("route: assignation manuelle ecrasee", "aihub/routing.py",
     "        if only_unassigned and st[\"agent_id\"] is not None:\n", "        if False:\n"),
    ("route: retrait d'agent non memorise", "aihub/store.py",
     "        _set_removed_agents(conn, plan_id, _removed_agents(conn, plan_id) + [agent_id])\n", ""),
    ("route: agent remis a la main reste retire", "aihub/store.py",
     "            if agent_id in removed:\n", "            if False:\n"),
    ("route: adaptation propose un agent retire", "aihub/orchestrator.py",
     ' if a["id"] not in plan["removed_agents"]]', "]"),
    ("route: raison gardee apres un choix manuel", "aihub/store.py",
     '            "UPDATE subtasks SET agent_id = ?, provider = ?, model = ?, assignment_reason = ? WHERE id = ?",\n            (agent_id, provider, model, reason if',
     '            "UPDATE subtasks SET agent_id = ?, provider = ?, model = ?, assignment_reason = COALESCE(?, assignment_reason) WHERE id = ?",\n            (agent_id, provider, model, reason if'),
    ("route: genre change ecrase un choix manuel", "aihub/routing.py",
     '    if subtask["agent_id"] is not None and subtask["assignment_reason"] is None:\n        return False\n',
     "    if False:\n        return False\n"),
    ("route: genre suit en mode manuel", "aihub/web/app.py",
     '                if conv["assignment_mode"] == "auto":\n                    routing.follow_kind',
     "                if True:\n                    routing.follow_kind"),
    ("route: genre inconnu garde tel quel", "aihub/orchestrator.py",
     'kind = item.get("kind") if item.get("kind") in KINDS else "autre"', 'kind = item.get("kind")'),
    ("route: genre et raison modifiables apres validation", "aihub/db.py",
     "# one in force), every column and exception kept.\n_SCHEMA_V28 = [\n    \"DROP TRIGGER subtasks_frozen_update\",\n    \"\"\"\n    CREATE TRIGGER subtasks_frozen_update\n"
     "    BEFORE UPDATE OF description, agent_id, provider, model, position, title, files_json,\n"
     "                     kind, assignment_reason ON subtasks\n",
     "# one in force), every column and exception kept.\n_SCHEMA_V28 = [\n    \"DROP TRIGGER subtasks_frozen_update\",\n    \"\"\"\n    CREATE TRIGGER subtasks_frozen_update\n"
     "    BEFORE UPDATE OF description, agent_id, provider, model, position, title, files_json ON subtasks\n"),
    # --- quota read without a request ---
    ("quota: pourcents pris pour des fractions", "aihub/providers/claude_code.py",
     'windows[name] = {"utilization": percent / 100,', 'windows[name] = {"utilization": percent,'),
    ("quota: seuil d'alerte ignore", "aihub/providers/claude_code.py",
     "elif worst >= warning_from or severities", "elif False or severities"),
    ("quota: limite atteinte non vue", "aihub/providers/claude_code.py",
     "    if worst >= 1:\n        status = \"rejected\"", "    if False:\n        status = \"rejected\""),
    ("quota: lecture sans --no-session-persistence", "aihub/providers/claude_code.py",
     '"--strict-mcp-config", "--no-session-persistence"]', '"--strict-mcp-config"]'),
    ("quota: relecture a chaque appel", "aihub/web/app.py",
     "if last is None or time.monotonic() - last >= QUOTA_REFRESH_S:", "if True:"),
    ("quota: erreur de relecture tue", "aihub/web/app.py",
     "                        except ProviderError as exc:\n                            errors[module.PROVIDER] = str(exc)\n",
     "                        except ProviderError as exc:\n                            pass\n"),
    ("cli: bypass sans confirmation tapee", "aihub/cli.py",
     'confirm = ask("Tapez « oui » pour confirmer : ").strip().lower() == "oui"',
     'confirm = True'),
    # --- Obsidian project notes ---
    ("obsidian: note hors du coffre", "aihub/obsidian.py",
     "    if path != root and root not in path.parents:\n", "    if False:\n"),
    ("obsidian: chemin absolu accepte", "aihub/obsidian.py",
     '    if Path(note_path).is_absolute() or note_path.strip().startswith(("/", "\\\\")):\n',
     "    if False:\n"),
    ("obsidian: autre fichier que .md", "aihub/obsidian.py",
     '    if path.suffix.lower() != ".md":\n', "    if False:\n"),
    ("obsidian: ajout qui ecrase la note", "aihub/obsidian.py",
     '        with open(path, "a", encoding="utf-8", newline="\\n") as fh:\n            if new:',
     '        with open(path, "w", encoding="utf-8", newline="\\n") as fh:\n            if new:'),
    ("obsidian: BOM en tete", "aihub/obsidian.py",
     '        with open(path, "a", encoding="utf-8", newline="\\n") as fh:\n            if new:',
     '        with open(path, "a", encoding="utf-8-sig", newline="\\n") as fh:\n            if new:'),
    ("obsidian: reecriture sans sauvegarde", "aihub/obsidian.py",
     "            backup.write_bytes(path.read_bytes())\n", ""),
    ("obsidian: sauvegarde dans le coffre", "aihub/web/jobs.py",
     'backups = Path(self.db_path).resolve().parent / "obsidian-sauvegardes"',
     'backups = Path(self.obsidian_vault) / "obsidian-sauvegardes"'),
    ("obsidian: plan note meme note coupee", "aihub/web/jobs.py",
     '            if not vault or not conversation["obsidian_note"]:\n                return None\n',
     '            if not vault or not conversation["obsidian_note"]:\n                conversation = dict(conversation, obsidian_note="Projets AI Hub/x.md")\n'),
    ("obsidian: plan execute jamais note", "aihub/web/jobs.py",
     "        note_error = self.note_plan(plan_id)\n", "        note_error = None\n"),
    ("obsidian: echec de note fait planter", "aihub/web/jobs.py",
     "        except obsidian.ObsidianError as exc:\n            return str(exc)\n",
     "        except ZeroDivisionError as exc:\n            return str(exc)\n"),
    ("obsidian: reecriture garde les plans annules", "aihub/web/jobs.py",
     'if plan["status"] in ("done", "error", "stopped") and ran:', 'if True:'),
    ("obsidian: reecriture oublie les reponses notees", "aihub/web/jobs.py",
     '                if message["noted_at"] and message["id"] not in in_plans:\n', "                if False:\n"),
    ("obsidian: reponse notee non retenue", "aihub/web/app.py",
     "            store.mark_noted(conn, mid)\n", ""),
    ("obsidian: message utilisateur notable", "aihub/web/app.py",
     '            if message["role"] != "assistant" or message["status"] != "ok":\n', "            if False:\n"),
    ("obsidian: reponse notee note coupee", "aihub/web/app.py",
     '            if not conv["obsidian_note"]:\n                raise StoreError("la note Obsidian n\'est pas activée pour cette conversation")\n'
     '            if message["role"]',
     '            if message["role"]'),
    ("obsidian: creation ecrite deux fois", "aihub/web/app.py",
     '                if note != conv["obsidian_note"]:\n', "                if True:\n"),
    ("obsidian: desactivation sans effet", "aihub/web/app.py",
     "                # The note stays in the vault; only the hub stops writing to it.\n                store.set_obsidian_note(conn, cid, None)\n",
     "                # The note stays in the vault; only the hub stops writing to it.\n                pass\n"),
    # --- phase 6: password, sessions, lockout, backups ---
    ("auth: tout mot de passe accepte", "aihub/auth.py",
     "    return hmac.compare_digest(actual, expected)\n", "    return True\n"),
    ("auth: mot de passe trop court accepte", "aihub/auth.py",
     "    if len(password) < MIN_PASSWORD:\n", "    if False:\n"),
    ("auth: client bloque teste le bon mot de passe", "aihub/auth.py",
     "        self.check_lock(conn, client)\n        if server_password is not None:\n",
     "        if server_password is not None:\n"),
    ("auth: echec non compte", "aihub/auth.py",
     "        left = self._record_failure(conn, client)\n        try:", "        left = 4\n        try:"),
    ("auth: blocage un essai trop tard", "aihub/auth.py",
     "            if mine >= self.policy.max_failures:\n", "            if mine > self.policy.max_failures:\n"),
    ("auth: pas de blocage global", "aihub/auth.py",
     "            if everyone >= self.policy.global_max_failures:\n", "            if False:\n"),
    ("auth: blocage jamais double", "aihub/auth.py",
     "timedelta(minutes=self.policy.lock_minutes) * 2 ** (count - 1)",
     "timedelta(minutes=self.policy.lock_minutes)"),
    ("auth: echecs jamais oublies", "aihub/auth.py",
     "        since = _iso(now - timedelta(minutes=self.policy.window_minutes))\n",
     "        since = _iso(now - timedelta(days=365))\n"),
    ("auth: vieux blocage allonge le suivant", "aihub/auth.py",
     '        recent = row is not None and _parse(row["until"]) > now - _MAX_LOCK\n',
     "        recent = row is not None\n"),
    ("auth: succes garde le blocage", "aihub/auth.py",
     '            conn.execute("DELETE FROM login_locks WHERE client = ?", (client,))\n', ""),
    ("auth: jeton stocke en clair", "aihub/auth.py",
     "(_token_hash(token), _iso(now), _iso(now),", "(token, _iso(now), _iso(now),"),
    ("auth: session jamais expiree", "aihub/auth.py",
     '        if _parse(row["expires_at"]) <= now:\n', "        if False:\n"),
    ("auth: session prolongee sans fin", "aihub/auth.py",
     "        return min(created + timedelta(days=self.policy.max_days), now + timedelta(days=self.policy.idle_days))\n",
     "        return now + timedelta(days=self.policy.idle_days)\n"),
    ("auth: changement de mot de passe garde les sessions", "aihub/auth.py",
     '        conn.execute("DELETE FROM sessions")\n', ""),
    ("web: api ouverte sans session", "aihub/web/app.py",
     "        if path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):\n",
     '        if path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES) or path.startswith("/api/"):\n'),
    ("web: ecriture sans en-tete X-AIHub", "aihub/web/app.py",
     ' and _headers(scope).get(CSRF_HEADER) != "1":', " and False:"),
    ("web: local seul laisse passer un relais", "aihub/web/app.py",
     "            if peer not in _LOOPBACK or relayed:\n", "            if peer not in _LOOPBACK:\n"),
    ("web: local seul laisse passer une autre machine", "aihub/web/app.py",
     "            if peer not in _LOOPBACK or relayed:\n", "            if relayed:\n"),
    ("web: X-Forwarded-For cru de n'importe qui", "aihub/web/app.py",
     "    if peer in _LOOPBACK and forwarded:\n", "    if forwarded:\n"),
    ("web: X-Forwarded-For ignore", "aihub/web/app.py",
     "    if peer in _LOOPBACK and forwarded:\n", "    if False:\n"),
    ("web: cookie lisible en JavaScript", "aihub/web/app.py", "httponly=True,", "httponly=False,"),
    ("web: cookie SameSite lax", "aihub/web/app.py", 'samesite="strict",', 'samesite="lax",'),
    ("web: cookie jamais Secure derriere https", "aihub/web/app.py",
     '                  or request.headers.get("x-forwarded-proto", "").lower() == "https")\n',
     "                  or False)\n"),
    ("web: deconnexion sans effet", "aihub/web/app.py",
     "                authority.logout(conn, request.cookies.get(auth.COOKIE))\n", "                pass\n"),
    ("web: blocage sans Retry-After", "aihub/web/app.py",
     '429, headers={"Retry-After": str(exc.retry_after_s)})', "429)"),
    ("web: suppression laisse les sauvegardes", "aihub/web/app.py",
     "errors = backup.purge_conversation(purge_folder(), cid)", "errors = {}"),
    ("web: sauvegarde non arretee avec le serveur", "aihub/web/app.py",
     "            for helper in (app.state.awake, app.state.backup):\n",
     "            for helper in (app.state.awake,):\n"),
    ("backup: purge sans secure_delete", "aihub/backup.py",
     'conn.execute("PRAGMA secure_delete = ON")', 'conn.execute("PRAGMA secure_delete = OFF")'),
    ("backup: purge sans cascade", "aihub/backup.py",
     '                conn.execute("PRAGMA foreign_keys = ON")\n', ""),
    ("backup: anciennes copies jamais effacees", "aihub/backup.py",
     "    for old in backups(folder)[:-keep] if keep > 0 else []:\n", "    for old in []:\n"),
    ("backup: copie du jour refaite", "aihub/backup.py",
     "    if target.exists():\n        return None\n", ""),
    ('refresh-v10: backup: fichier partiel laisse', 'aihub/backup.py', '            temporary.unlink(missing_ok=True)\n            pieces_temporary.unlink(missing_ok=True)\n', '            pass\n'),
    ("backup: copie en mode WAL", "aihub/backup.py",
     '                copy.execute("PRAGMA journal_mode = DELETE")\n', ""),
    ("backup: purge sans attendre la copie", "aihub/backup.py",
     '    with _LOCK:\n        return _purge(folder, "DELETE FROM conversations',
     '    if True:\n        return _purge(folder, "DELETE FROM conversations'),
    # --- projects, and reading between conversations ---
    ("mcp: conversation privee lit les autres", "aihub/project_mcp.py",
     '        if not me["shared"]:\n', "        if False:\n"),
    ("mcp: conversation hors projet acceptee", "aihub/project_mcp.py",
     '        if me["project_id"] is None:\n', "        if False:\n"),
    ("mcp: conversations privees visibles", "aihub/project_mcp.py",
     '"SELECT * FROM conversations WHERE project_id = ? AND shared = 1 AND id <> ? "',
     '"SELECT * FROM conversations WHERE project_id = ? AND id <> ? "'),
    ("mcp: autres projets visibles", "aihub/project_mcp.py",
     '"SELECT * FROM conversations WHERE project_id = ? AND shared = 1 AND id <> ? "',
     '"SELECT * FROM conversations WHERE ? IS NOT NULL AND shared = 1 AND id <> ? "'),
    ("mcp: elle se voit elle-meme", "aihub/project_mcp.py",
     '"SELECT * FROM conversations WHERE project_id = ? AND shared = 1 AND id <> ? "',
     '"SELECT * FROM conversations WHERE project_id = ? AND shared = 1 AND ? IS NOT NULL "'),
    ("mcp: base ouverte en ecriture", "aihub/project_mcp.py", '+ "?mode=ro"', '+ "?mode=rwc"'),
    ("mcp: reponses en echec montrees", "aihub/project_mcp.py",
     " AND (role = 'user' OR status = 'ok') ORDER BY id\"", ' ORDER BY id"'),
    ("mcp: lecture hors des visibles", "aihub/project_mcp.py",
     "            if cid not in visible:\n", "            if False:\n"),
    ("mcp: lecture de soi-meme", "aihub/project_mcp.py",
     "            if cid == self.conversation_id:\n", "            if False:\n"),
    ("mcp: majuscules comptees", "aihub/project_mcp.py",
     '_WORD.findall(str(text or "").casefold())', '_WORD.findall(str(text or ""))'),
    ("mcp: mots courants comptes", "aihub/project_mcp.py",
     "        if len(word) < 3 or word in _STOP or word.isdigit():\n", "        if len(word) < 3:\n"),
    ("mcp: pluriel distingue", "aihub/project_mcp.py",
     '        if len(word) > 4 and word[-1] in "sx":\n', "        if False:\n"),
    ("mcp: feminin distingue", "aihub/project_mcp.py",
     '        if len(word) > 4 and word[-1] == "e":', "        if False:"),
    ("mcp: mot repete sans plafond", "aihub/project_mcp.py",
     "* n * (_K1 + 1) / (n + norm)", "* n"),
    ("mcp: passages faibles gardes", "aihub/project_mcp.py",
     "        return [p for score, _, p in scored if score >= RELATIVE * best]\n",
     "        return [p for score, _, p in scored]\n"),
    ("mcp: passage sans sa reponse", "aihub/project_mcp.py",
     "            lines.append(f\"  → réponse : {short(messages[index + 1]['content'])}\")\n", "            pass\n"),
    ("mcp: titre cite ignore", "aihub/project_mcp.py",
     ' or (len(row["title"]) >= 4 and row["title"].casefold() in folded)}', "}"),
    ("mcp: un seul mot commun suffit", "aihub/project_mcp.py",
     'strong = [p for p in ranked if len(wanted & set(terms(p[3]["content"]))) >= min_terms][:limit]',
     "strong = ranked[:limit]"),
    ("acces: passages automatiques pour les invites du hub", "aihub/project_access.py",
     'found = access.closest(prompt, AUTO_BUDGET) if automatic else ""', "found = access.closest(prompt, AUTO_BUDGET)"),
    ("acces: passages jamais joints", "aihub/runner.py",
     "            prompt=sent,\n", "            prompt=self.prompt,\n"),
    ("acces: invites du hub traitees comme l'utilisateur", "aihub/runner.py",
     "automatic=self.record_prompt,", "automatic=True,"),
    ("acces: consigne sans la liste", "aihub/project_access.py",
     '        listed = f"Autres conversations du projet : {listed}"', '        listed = f"Autres conversations : "'),
    ("cc: tours non releves", "aihub/providers/claude_code.py",
     '                    result.num_turns = event["num_turns"]\n', "                    pass\n"),
    ("cc: appels d'outils non releves", "aihub/providers/claude_code.py",
     '                        result.tool_calls.append(str(block.get("name") or "?"))\n', "                        pass\n"),
    ("mcp: limite de recherche ignoree", "aihub/project_mcp.py",
     "        limit = max(1, min(int(limit or SEARCH_DEFAULT), SEARCH_MAX))\n        conn = self._connect()\n        try:\n            ranked",
     "        limit = SEARCH_MAX\n        conn = self._connect()\n        try:\n            ranked"),
    ("mcp: lecture sans budget", "aihub/project_mcp.py",
     "            if used + len(block) > budget:\n", "            if False:\n"),
    ("mcp: long message abandonne", "aihub/project_mcp.py",
     '                    parts.append(block[:budget] + " […coupé]")\n                    last = position\n',
     "                    pass\n"),
    ("mcp: version du protocole imposee", "aihub/project_mcp.py",
     '"protocolVersion": asked or PROTOCOL', '"protocolVersion": PROTOCOL'),
    ("mcp: notification repondue", "aihub/project_mcp.py",
     "    if mid is None:\n        return None  # notifications", "    if False:\n        return None  # notifications"),
    ("mcp: refus presente comme reussite", "aihub/project_mcp.py",
     "            text, error = str(exc), True\n        except (sqlite3.Error",
     "            text, error = str(exc), False\n        except (sqlite3.Error"),
    ("acces: conversation privee recoit le serveur", "aihub/project_access.py",
     '    if not own and (not project or conversation["project_id"] is None or not conversation["shared"]):\n',
     '    if not own and (not project or conversation["project_id"] is None):\n'),
    ("acces: hors projet recoit le serveur", "aihub/project_access.py",
     '    if not own and (not project or conversation["project_id"] is None or not conversation["shared"]):\n',
     '    if not own and (not project or not conversation["shared"]):\n'),
    ("acces: python sans -I", "aihub/project_access.py",
     '"args": ["-I", str(SERVER_SCRIPT),', '"args": [str(SERVER_SCRIPT),'),
    ("acces: runner sans serveur", "aihub/runner.py",
     "            mcp_config=given.mcp_config,\n", "            mcp_config=None,\n"),
    ("acces: runner sans consigne", "aihub/runner.py",
     "                                                 project_access.instructions(self.conn, conversation, project=project)),",
     "                                                 None),"),
    ("acces: sous-taches sans serveur", "aihub/orchestrator.py",
     "\n                mcp_config=given.mcp_config, auto_allow", "\n                mcp_config=None, auto_allow"),
    ("cc: tout outil autorise sans demander", "aihub/providers/claude_code.py",
     "        if _allowed(ask.tool_name, self.auto_allow):\n", "        if True:\n"),
    ("cc: outils du hub demandes", "aihub/providers/claude_code.py",
     "        if _allowed(ask.tool_name, self.auto_allow):\n", "        if False:\n"),
    ("cc: --allowedTools absent", "aihub/providers/claude_code.py",
     '        if self.auto_allow:\n            args += ["--allowedTools"',
     '        if False:\n            args += ["--allowedTools"'),
    ("store: projet supprime emporte ses conversations", "aihub/store.py",
     'conn.execute("UPDATE conversations SET project_id = NULL WHERE project_id = ?", (project_id,))',
     'conn.execute("DELETE FROM conversations WHERE project_id = ?", (project_id,))'),
    ("store: projet supprime pendant une requete", "aihub/store.py",
     '            if active_request(conn, cid) is not None:\n                raise Busy("une conversation du projet a une requête',
     '            if False:\n                raise Busy("une conversation du projet a une requête'),
    ("db: projet efface ses conversations en cascade", "aihub/db.py",
     "REFERENCES projects(id) ON DELETE SET NULL", "REFERENCES projects(id) ON DELETE CASCADE"),
    ("db: conversations privees par defaut", "aihub/db.py",
     "shared INTEGER NOT NULL DEFAULT 1", "shared INTEGER NOT NULL DEFAULT 0"),
    ("projet: dossier du projet ignore", "aihub/web/app.py",
     '            workdir = store.get_project(conn, body.project_id)["workdir"] or ""\n',
     '            workdir = ""\n'),
    ("projet: conversation creee hors du projet", "aihub/web/app.py",
     "            project_id=body.project_id,\n        )", "        )"),
    ("projet: suppression laisse le projet dans les sauvegardes", "aihub/web/app.py",
     "        errors.update(backup.purge_project(folder, pid))\n", ""),
    ("projet: suppression laisse ses conversations dans les sauvegardes", "aihub/web/app.py",
     "            errors.update(backup.purge_conversation(folder, cid))\n", "            pass\n"),
    ("projet: sortie du projet ignoree", "aihub/web/app.py",
     "            if body.leave_project:\n                store.move_conversation(conn, cid, None)\n",
     "            if body.leave_project:\n                pass\n"),
    ("projet: partage ignore", "aihub/web/app.py",
     "                store.set_shared(conn, cid, body.shared)\n", "                pass\n"),
    # --- end of phase 6: devices, keeping awake, start with Windows, restore ---
    ("appareils: aucun marque comme celui-ci", "aihub/auth.py",
     '"current": r["token_hash"] == mine}', '"current": False}'),
    ("appareils: empreinte du jeton montree", "aihub/auth.py",
     'return [{"id": r["id"], "created_at"', 'return [{"id": r["id"], "token_hash": r["token_hash"], "created_at"'),
    ("appareils: deconnecter les autres deconnecte aussi celui-ci", "aihub/auth.py",
     '"DELETE FROM sessions WHERE token_hash <> ? AND user_id = "',
     '"DELETE FROM sessions WHERE ? IS NOT NULL AND user_id = "'),
    ("appareils: session inconnue acceptee", "aihub/web/app.py",
     "            if not authority.close_session(conn, sid, request.cookies.get(auth.COOKIE)):\n                raise NotFound",
     "            if False:\n                raise NotFound"),
    ("eveil: veille jamais retenue", "aihub/eveil.py",
     "self.set_state(ES_CONTINUOUS | ES_SYSTEM_REQUIRED if hold else ES_CONTINUOUS)",
     "self.set_state(ES_CONTINUOUS)"),
    ("eveil: Windows appele a chaque tour", "aihub/eveil.py",
     "        if hold == self.holding or self.set_state is None:\n", "        if self.set_state is None:\n"),
    ("eveil: veille retenue apres l'arret", "aihub/eveil.py",
     "            self._apply(False)  # the server stops", "            pass  # the server stops"),
    ("eveil: verification cassee retient la veille", "aihub/eveil.py",
     "                    busy = False\n", "                    busy = True\n"),
    ("eveil: garde non arretee avec le serveur", "aihub/web/app.py",
     "            for helper in (app.state.awake, app.state.backup):\n",
     "            for helper in (app.state.backup,):\n"),
    ("eveil: cli sans --laisser-dormir", "aihub/cli.py",
     '("keep_awake", False if args.let_sleep else None)', '("keep_awake", None)'),
    # --- phase 8: settings ---
    ("config: reglage inconnu accepte", "aihub/config.py",
     '    if unknown:\n        raise ConfigError(f"réglage inconnu', '    if False:\n        raise ConfigError(f"réglage inconnu'),
    ("config: chemin relatif accepte", "aihub/config.py", "    if not path.is_absolute():\n", "    if False:\n"),
    ("config: dossier absent accepte", "aihub/config.py", "    if must_exist and not path.is_dir():\n", "    if False:\n"),
    ("config: bornes ignorees", "aihub/config.py", "    if not low <= value <= high:\n", "    if False:\n"),
    ("config: oui/non pris pour un nombre", "aihub/config.py",
     "    if isinstance(value, bool) or not isinstance(value, (int, float)):\n",
     "    if not isinstance(value, (int, float)):\n"),
    ("config: defaut hors des modeles proposes", "aihub/config.py",
     '        if merged[name] not in out["models"]:\n', "        if False:\n"),
    ("config: ordre des modeles libre", "aihub/config.py",
     'out["models"] = [m for m in MODELS if m in models]', 'out["models"] = list(models)'),
    ("reglages: sans mot de passe", "aihub/web/app.py",
     "        if authority is not None:\n            # A stolen session", "        if False:\n            # A stolen session"),
    ("reglages: jamais ecrits", "aihub/web/app.py",
     "            config.save(config_path, to_file)  # written first", "            pass  # written first"),
    ("reglages: options du lancement ecrites", "aihub/web/app.py",
     "to_file = config.validate(body.changes, base=config.load(config_path))", "to_file = in_force"),
    ("reglages: jamais appliques", "aihub/web/app.py",
     "            apply(in_force)\n        return get_config()", "            pass\n        return get_config()"),
    ("reglages: dossiers autorises figes", "aihub/web/app.py",
     "        return places.Places(current().allowed_roots)\n", "        return places.Places()\n"),
    ("reglages: coffre non transmis aux taches", "aihub/web/app.py",
     "        jobs.obsidian_vault = new.obsidian_vault\n", ""),
    ("reglages: regles de session figees", "aihub/web/app.py",
     "            authority.policy = policy_of(new)\n", "            pass\n"),
    ("reglages: ancienne sauvegarde jamais arretee", "aihub/web/app.py",
     '            if app.state.backup is not None and running["started"]:\n                app.state.backup.stop()',
     "            if False:\n                app.state.backup.stop()"),
    ("reglages: ancienne garde d'eveil jamais arretee", "aihub/web/app.py",
     '            if app.state.awake is not None and running["started"]:\n                app.state.awake.stop()',
     "            if False:\n                app.state.awake.stop()"),
    ("reglages: seuil du quota ignore", "aihub/web/app.py",
     "env=run_env, warning_from=current().quota_warning / 100)", "env=run_env)"),
    ("reglages: modele retire encore choisissable", "aihub/web/app.py",
     "        if model not in offered:\n", "        if False:\n"),
    ("reglages: purge oubliee sauvegardes coupees", "aihub/web/app.py",
     "errors = backup.purge_conversation(purge_folder(), cid)",
     'errors = backup.purge_conversation(backup_folder() or "", cid)'),
    ("cli: options du lancement ignorees", "aihub/cli.py",
     "settings = config.validate(overrides, base=config.load(config_path))", "settings = config.load(config_path)"),
    ("cli: fichier de reglages ignore", "aihub/cli.py",
     "settings = config.validate(overrides, base=config.load(config_path))", "settings = config.validate(overrides)"),
    # --- phase 11: memory (RAG) ---
    ("memoire: secure-delete absent", "aihub/db.py",
     '    "INSERT INTO memory(memory, rank) VALUES (\'secure-delete\', 1)",\n', ""),
    ("memoire: reponse indexee en cours d'ecriture", "aihub/db.py",
     "    CREATE TRIGGER memory_message_done AFTER UPDATE OF status, content ON messages\n"
     "    WHEN NEW.status = 'ok' AND",
     "    CREATE TRIGGER memory_message_done AFTER UPDATE OF status, content ON messages\n"
     "    WHEN NEW.status IN ('ok', 'running') AND"),
    ("memoire: reponse en echec indexee", "aihub/db.py",
     "    WHEN NEW.content IS NOT NULL AND NEW.content <> '' AND (NEW.role = 'user' OR NEW.status = 'ok')\n",
     "    WHEN NEW.content IS NOT NULL AND NEW.content <> ''\n"),
    ("memoire: suppression laisse l'index", "aihub/db.py",
     "    CREATE TRIGGER memory_message_delete AFTER DELETE ON messages\n    BEGIN\n"
     "        DELETE FROM memory WHERE message_id = OLD.id;\n",
     "    CREATE TRIGGER memory_message_delete AFTER DELETE ON messages\n    BEGIN\n        SELECT 1;\n"),
    ("memoire: comptes rendus non indexes", "aihub/db.py",
     "        SELECT NEW.result, p.conversation_id, NEW.id, 'subtask' FROM plans p WHERE p.id = NEW.plan_id;\n",
     "        SELECT NEW.result, p.conversation_id, NEW.id, 'subtask' FROM plans p WHERE 0;\n"),
    ("memoire: ancienne base non indexee", "aihub/db.py",
     "    SELECT content, conversation_id, id, role FROM messages\n    WHERE content IS NOT NULL",
     "    SELECT content, conversation_id, id, role FROM messages\n    WHERE 0 AND content IS NOT NULL"),
    ("memoire: un seul mot suffit dans la recherche", "aihub/memory.py",
     'return (" AND " if every else " OR ").join(', 'return " OR ".join('),
    ("memoire: echange en echec redonne", "aihub/memory.py",
     "\"AND content <> '' AND (role = 'user' OR status = 'ok') ORDER BY id DESC LIMIT ?\",",
     "\"AND content <> '' ORDER BY id DESC LIMIT ?\","),
    ("memoire: passages hors budget", "aihub/memory.py",
     "        if used + cost > budget_tokens:\n            break\n", ""),
    ("memoire: passages deja dans le paquet", "aihub/memory.py",
     '        if row["message_id"] is not None and row["message_id"] in exclude:\n',
     "        if False:\n"),
    ("memoire: resume refait sans nouveau message", "aihub/memory.py",
     "    if not new:\n        return current", "    if False:\n        return current"),
    ("memoire: resume depuis le debut", "aihub/memory.py",
     '    after = current["up_to_message"] if current else 0\n', "    after = 0\n"),
    ("memoire: plafond jamais depasse", "aihub/memory.py",
     '    package["over"] = package["tokens"] > budget\n', '    package["over"] = False\n'),
    ("memoire: compression sans l'IA de resume", "aihub/memory.py",
     '    if package_tokens(out) > budget and out["summary"] and resummarize is not None:\n',
     "    if False:\n"),
    ("memoire: echanges recents coupes d'abord", "aihub/memory.py",
     '        out["exchanges"].pop(0)\n', '        out["exchanges"].pop()\n'),
    ("runner: depart demande ignore", "aihub/runner.py",
     "            package = memory.load_package(conversation) if self.resume else None\n",
     "            package = None\n"),
    ("runner: reprise malgre le depart", "aihub/runner.py",
     "session_scope=self.session_scope) if self.resume and package is None else None", "session_scope=self.session_scope) if self.resume else None"),
    ("runner: paquet garde apres usage", "aihub/runner.py",
     "                memory.store_package(self.conn, self.conversation_id, None)  # done", "                pass  # done"),
    ("runner: session perdue sans relance", "aihub/runner.py",
     "            if (resume_id and result.status == \"error\" and providers.lost_session(self.provider, result.error)\n",
     "            if (False\n"),
    ("runner: la demande se trouve elle-meme", "aihub/runner.py",
     ' | {self.request_id}', ""),
    ("runner: depart sans outil de recherche", "aihub/runner.py",
     "self.settings, own=package is not None, commands", "self.settings, own=False, commands"),
    ("runner: resume avec des outils", "aihub/runner.py",
     'model=providers.default_model(provider, model), tools=[], env=env,',
     'model=providers.default_model(provider, model), tools=None, env=env,'),
    ("runner: resume rate fait echouer le depart", "aihub/runner.py",
     "        except ProviderError:\n            # No summary", "        except ZeroDivisionError:\n            # No summary"),
    ("cc: erreurs de la CLI ignorees", "aihub/providers/claude_code.py",
     '            if not text.strip() and isinstance(final.get("errors"), list):\n', "            if False:\n"),
    ("depart: choix inconnu apres le resume", "aihub/web/app.py",
     '        if body.decision not in (None, "raise", "auto", "custom"):\n', "        if False:\n"),
    ("depart: plafond ignore", "aihub/web/app.py",
     '            if package["over"] and body.decision != "custom":\n', "            if False:\n"),
    ("depart: paquet a la main au-dela du plafond", "aihub/web/app.py",
     "                if package[\"over\"] and not body.allow_over:\n", "                if False:\n"),
    ("depart: plafond augmente non retenu", "aihub/web/app.py",
     "                    if body.remember:\n", "                    if False:\n"),
    ("depart: pendant une requete", "aihub/web/app.py",
     "            raise Busy(\"une requête est en cours : attendez qu'elle ait fini\")\n        if body.decision",
     "            pass\n        if body.decision"),
    ("quota: seuil fixe a 80", "aihub/providers/claude_code.py",
     "    elif worst >= warning_from or", "    elif worst >= _WARNING_FROM or"),
    ("cli: fichier pid perime cru", "aihub/cli.py",
     "if pid != os.getpid() and _process_alive(pid) else None", "if pid != os.getpid() else None"),
    ("cli: fichier pid laisse", "aihub/cli.py", "                marker.unlink()\n", "                pass\n"),
    ("cli: second serveur accepte", "aihub/cli.py", "    if already:\n", "    if False:\n"),
    ("cli: journal garde la sortie", "aihub/cli.py",
     "                sys.stdout, sys.stderr = saved\n", "                pass\n"),
    ("cli: raccourci sans guillemets", "aihub/cli.py",
     "line = subprocess.list2cmdline([", 'line = " ".join(['),
    ("restaurer: serveur en marche ignore", "aihub/cli.py",
     '    if server:\n        out(f"Le serveur tourne', '    if False:\n        out(f"Le serveur tourne'),
    ("restaurer: sans confirmation", "aihub/cli.py",
     'if ask("Tapez « oui » pour restaurer : ").strip().lower() != "oui":', "if False:"),
    ('refresh-v10: restaurer: ancienne base perdue', 'aihub/backup.py', '            copy_database(target_db, keep)\n', '            pass\n'),
    ("web: fichiers de l'appli gardes en cache", "aihub/web/app.py",
     '        response.headers["Cache-Control"] = "no-cache"\n        return response\n',
     "        return response\n"),
    ("backup: vieille copie sans table projets", "aihub/backup.py",
     "                if table and conn.execute(", "                if False and conn.execute("),
    # « cli: serveur sans mot de passe » retired (11/10): a server without an owner now starts, to be
    # created from its page on this computer; that guard is « roles: serveur cree depuis ailleurs ».
    ("cli: sans mot de passe sur une autre adresse", "aihub/cli.py",
     "        if args.host not in _LOCAL_HOSTS:\n", "        if False:\n"),
    ("cli: sans mot de passe ouvert aux relais", "aihub/cli.py",
     "local_only=policy is None,", "local_only=False,"),
    ("cli: mot de passe tape une seule fois", "aihub/cli.py",
     '    if first != secret("Le même, une seconde fois : "):\n',
     '    secret("Le même, une seconde fois : ")\n    if False:\n'),
    ("obsidian: reecriture pendant une requete", "aihub/web/app.py",
     "        if jobs.running(cid):\n            raise Busy(\"une requête est en cours : attendez qu'elle ait fini pour réécrire la note\")\n",
     ""),
    # --- phase 9: Codex ---
    ("codex: commande acceptee sans demander", "aihub/providers/codex.py",
     'decision = self._decide("Bash", data, rid, result)\n            server.write({"id": rid, "result": {"decision": "accept" if decision.allow else "decline"}})',
     'decision = self._decide("Bash", data, rid, result)\n            server.write({"id": rid, "result": {"decision": "accept"}})'),
    ("codex: fichiers acceptes sans demander", "aihub/providers/codex.py",
     'rid, result)\n            server.write({"id": rid, "result": {"decision": "accept" if decision.allow else "decline"}})\n        elif method == "mcpServer',
     'rid, result)\n            server.write({"id": rid, "result": {"decision": "accept"}})\n        elif method == "mcpServer'),
    ("codex: rappel casse approuve", "aihub/providers/codex.py",
     'decision = Decision(False, f"validation impossible : {exc}")', "decision = Decision(True)"),
    ("codex: reponse non Decision acceptee", "aihub/providers/codex.py",
     "            if not isinstance(decision, Decision):\n", "            if False:\n"),
    ("codex: personne pour valider = accepte", "aihub/providers/codex.py",
     "decision = Decision(False, \"aucune personne n'est là pour valider : refus par défaut\")", "decision = Decision(True)"),
    ("codex: requete de jeton servie", "aihub/providers/codex.py",
     'server.write({"id": rid, "error": {"code": -32601, "message": f"non pris en charge : {method}"}})',
     'server.write({"id": rid, "result": {}})'),
    ("codex: droits supplementaires accordes", "aihub/providers/codex.py",
     '{"permissions": {}, "scope": "turn"}', '{"permissions": params, "scope": "turn"}'),
    ("codex: tout serveur MCP autorise", "aihub/providers/codex.py",
     "allowed = any(name == prefix or name.startswith(prefix + \"__\") for name in self.auto_allow)",
     "allowed = True"),
    ("codex: serveurs MCP de l'utilisateur gardes", "aihub/providers/codex.py",
     'args += ["-c", f"mcp_servers.{key}.enabled=false"]', "pass"),
    ("codex: apps et plugins gardes", "aihub/providers/codex.py",
     '_DISABLED_FEATURES = ("apps", "plugins")', "_DISABLED_FEATURES = ()"),
    ("codex: mode manuel sans demande", "aihub/providers/codex.py",
     '"manual": ("read-only", "on-request"),', '"manual": ("workspace-write", "never"),'),
    ("codex: plan en ecriture", "aihub/providers/codex.py",
     '"plan": ("read-only", "never"),', '"plan": ("workspace-write", "never"),'),
    ("codex: outils restreints ignores", "aihub/providers/codex.py",
     'mode = "plan" if self.tools is not None else self.permission_mode', "mode = self.permission_mode"),
    ("codex: quota atteint ignore", "aihub/providers/codex.py",
     'if snapshot.get("rateLimitReachedType") or worst >= 1:', "if worst >= 1:"),
    ("codex: seuil d'alerte ignore", "aihub/providers/codex.py",
     "    elif worst >= warning_from:\n", "    elif False:\n"),
    ("codex: commentaire garde comme reponse", "aihub/providers/codex.py",
     'finals = [text for phase, text in messages if phase == "final_answer"]', "finals = [text for phase, text in messages]"),
    ("codex: session perdue non reconnue", "aihub/providers/codex.py",
     'return "no rollout found" in (error or "").lower()', "return False"),
    ("codex: tour echoue pris pour reussi", "aihub/providers/codex.py",
     '        if turn.get("status") != "completed":\n', "        if False:\n"),
    ("codex: arret sans tuer", "aihub/providers/codex.py",
     "            server = self._server\n        if server is not None:\n", "            server = self._server\n        if False:\n"),
    ("codex: delai ignore", "aihub/providers/codex.py",
     "            timer.start()\n        outcome = {}", "            pass\n        outcome = {}"),
    ("codex: appels du modele non comptes", "aihub/providers/codex.py",
     "result.num_turns = (result.num_turns or 0) + 1", "pass"),
    ("codex: schema non transmis", "aihub/providers/codex.py",
     '                    turn["outputSchema"] = self.json_schema\n', "                    pass\n"),
    ("prov: modele par defaut envoye", "aihub/providers/__init__.py",
     "return None if model in (None, module(provider).DEFAULT_MODEL) else model", "return model"),
    ("prov: Codex non propose accepte", "aihub/providers/__init__.py",
     "    if given is False:\n", "    if False:\n"),
    ("runner: changement d'IA reprend la session de l'autre", "aihub/runner.py",
     "resume_id = last[1] if last and last[0] == self.provider else None", "resume_id = last[1] if last else None"),
    ("runner: changement d'IA sans memoire", "aihub/runner.py",
     "            if switched:\n                # Another AI", "            if False:\n                # Another AI"),
    ("runner: quota range sous Claude", "aihub/runner.py",
     "self.conn, self.provider, quota.status,", 'self.conn, "claude_code", quota.status,'),
    ("orch: seul le premier fichier reserve", "aihub/orchestrator.py",
     'named = request.input.get("file_paths") or [request.input.get("file_path")]', 'named = [request.input.get("file_path")]'),
    ("orch: sous-tache Codex lancee sur Claude", "aihub/orchestrator.py",
     'provider = subtask["provider"] or claude_code.PROVIDER', "provider = claude_code.PROVIDER"),
    ("route: modele non propose choisi", "aihub/routing.py",
     "        if offered is not None and rule.model not in offered:\n", "        if False:\n"),
    ("web: modeles GPT proposes sans Codex", "aihub/web/app.py",
     "return [m for m in s.models if config.provider_of(m) != codex.PROVIDER or m in usable]", "return list(s.models)"),
    ("web: vrai Codex cherche sous un faux Claude", "aihub/web/app.py",
     "                    if command is None or given_paths.get(codex.PROVIDER) else False),\n",
     "                    ),\n"),
    ("web: table jamais informee", "aihub/web/app.py",
     "        routing.set_available(connected or {claude_code.PROVIDER: None})\n", ""),
    ("web: agent GPT cree sous Claude", "aihub/web/app.py",
     "return store.ensure_agent(conn, config.provider_of(model), model)", 'return store.ensure_agent(conn, "claude_code", model)'),
    ("store: session d'un seul fournisseur", "aihub/store.py",
     "        WHERE conversation_id = ? AND session_id IS NOT NULL\n",
     "        WHERE conversation_id = ? AND session_id IS NOT NULL AND provider = 'claude_code'\n"),
    ("web: changement d'IA ignore", "aihub/web/app.py",
     "                else:\n                    store.update_conversation(conn, cid, agent_id=agent_id)\n",
     "                else:\n                    pass\n"),
    ("refresh-v10: web: changement d'IA sans controle du modele", 'aihub/web/app.py', "                agent_id = agent_for(conn, body.model, isolated=isolation.effective(conn, store.get_conversation(conn, cid))['isolated'])", '                agent_id = store.ensure_agent(conn, config.provider_of(body.model), body.model)'),
    ("codex: liste des modeles sans params", "aihub/providers/codex.py",
     '"model/list", {}, env=env', '"model/list", env=env'),
    ("runner: la demande parmi les derniers echanges", "aihub/runner.py",
     "s.rag_exchanges, before=self.request_id),", "s.rag_exchanges),"),
    ("runner: la demande dans le resume", "aihub/runner.py",
     "summarize, before=self.request_id)", "summarize)"),
    # --- autonomous work ---
    ("auto: fini sans verification", "aihub/autonomous.py",
     'if state == "fini" and (check is None or check[0] == 0):', 'if state == "fini":'),
    ("auto: verification seule suffit", "aihub/autonomous.py",
     'if state == "fini" and (check is None or check[0] == 0):', 'if check is None or check[0] == 0:'),
    ("auto: limite de temps ignoree", "aihub/autonomous.py", "                if left <= 0:\n                    break\n",
     "                pass\n"),
    ("auto: tour en echec ignore", "aihub/autonomous.py",
     '                if result.status != "ok":\n                    status, error = "error", result.error\n',
     '                if False:\n                    status, error = "error", result.error\n'),
    ("auto: arret ignore apres un tour", "aihub/autonomous.py",
     '                if self._stopping():\n                    status = "stopped"\n                    break\n                if state',
     '                if state'),
    ("auto: arret du tour en cours ignore", "aihub/autonomous.py",
     "        if request is not None:\n            request.stop()\n", ""),
    ("auto: verification sans confirmation", "aihub/autonomous.py",
     "    if command and not confirm_check:\n", "    if False:\n"),
    ("auto: commande sur plusieurs lignes", "aihub/autonomous.py",
     '    if command and ("\\n" in command or len(command) > 1000):\n', "    if False:\n"),
    ('refresh-v10: auto: verification sans delai', 'aihub/autonomous.py', '            proc.wait(timeout=timeout)\n', '            proc.wait()\n'),
    ("auto: resultat de la verification non transmis", "aihub/autonomous.py",
     '        parts.append(f"Vérification {verdict}. Fin de sa sortie :\\n{output or \'(rien)\'}")\n', ""),
    ("auto: derniere ligne d'etat ignoree", "aihub/autonomous.py",
     'return "fini" if found[-1].upper() == "FINI" else "suivre"',
     'return "fini" if found[0].upper() == "FINI" else "suivre"'),
    ("auto: tours non enregistres", "aihub/autonomous.py",
     '        self.conn.execute("UPDATE autonomous_runs SET rounds = ? WHERE id = ?", (number, self.run_id))\n', ""),
    ("auto: course coupee laissee en cours", "aihub/web/jobs.py",
     "            autonomous.recover(conn)", "            pass"),
    ("auto: arret juste avant la verification", "aihub/autonomous.py",
     "        if stopped:  # a stop that came just before", "        if False:  # a stop that came just before"),
    ("auto: limites non controlees", "aihub/autonomous.py",
     "        self.check_command = check_limits(max_rounds, max_minutes, check_command, confirm_check)\n",
     "        self.check_command = check_command\n"),
    ("auto: fin non signalee", "aihub/web/jobs.py",
     '            self._alert("autonomous_done"', '            (lambda *a: None)("autonomous_done"'),
    ("auto: lance en orchestration", "aihub/web/app.py",
     '        if conv["mode"] != "single":\n            raise StoreError("le travail autonome',
     '        if False:\n            raise StoreError("le travail autonome'),
    # --- scheduled tasks ---
    ("plan: heure deja passee reprise", "aihub/schedule.py", "        if candidate <= local:\n            continue\n",
     ""),
    ("plan: jours de la semaine ignores", "aihub/schedule.py",
     'if rule["every"] == "daily" or candidate.weekday() in days:', "if True:"),
    ("plan: occupee lancee quand meme", "aihub/schedule.py",
     "                except store.Busy:\n                    conn.execute(\"UPDATE schedules SET next_run_at",
     "                except ZeroDivisionError:\n                    conn.execute(\"UPDATE schedules SET next_run_at"),
    ("plan: retard non avance", "aihub/schedule.py",
     "                             (_iso(now + BUSY_RETRY), task[\"id\"]))", "                             (_iso(now), task[\"id\"]))"),
    ("plan: rattrapage de chaque heure manquee", "aihub/schedule.py",
     '"next_run_at = ? WHERE id = ?", (_iso(now), _iso(next_run(task, now)), task["id"]))',
     '"next_run_at = ? WHERE id = ?", (_iso(now), _iso(next_run(task, _utc(task["next_run_at"]))), task["id"]))'),
    ("plan: desactivee lancee", "aihub/schedule.py",
     '"SELECT * FROM schedules WHERE enabled = 1 AND next_run_at <= ?', '"SELECT * FROM schedules WHERE next_run_at <= ?'),
    ("plan: reprise depuis une vieille date", "aihub/schedule.py",
     "nxt = _iso(next_run(task, now or datetime.now(timezone.utc))) if enabled else task[\"next_run_at\"]",
     "nxt = task[\"next_run_at\"]"),
    ("plan: verification non confirmee", "aihub/schedule.py",
     "        command = autonomous.check_limits(max_rounds, max_minutes, check_command, confirm_check)\n",
     "        command = check_command\n"),
    ("plan: tache en orchestration", "aihub/schedule.py",
     '    if conversation["mode"] != "single":\n', "    if False:\n"),
    ("plan: fin de tache non enregistree", "aihub/web/jobs.py",
     "                self._record_task_end(job, done)  # before", "                pass  # before"),
    ("plan: double alerte", "aihub/web/jobs.py",
     "            self._end_of_task(job, done)\n            return\n", "            self._end_of_task(job, done)\n"),
    ("plan: lancee sans sa tache", "aihub/web/app.py",
     'jobs.start(conv["id"], conv["agent_id"], task["prompt"], schedule_id=task["id"])',
     'jobs.start(conv["id"], conv["agent_id"], task["prompt"])'),
    # --- tools ---
    ("outils: coffre hors des murs", "aihub/vault_mcp.py",
     "        if not target.is_relative_to(self.root):\n", "        if False:\n"),
    ("outils: dossiers caches lus", "aihub/vault_mcp.py",
     '        if any(part.startswith(".") for part in target.relative_to(self.root).parts):\n', "        if False:\n"),
    ("outils: serveur non lecture seule autorise", "aihub/tools.py",
     '        if server["read_only"]:\n            auto.append', '        if True:\n            auto.append'),
    ("outils: env accepte", "aihub/tools.py",
     'unknown = set(entry) - {"name", "command", "args", "description", "read_only"}',
     'unknown = set(entry) - {"name", "command", "args", "description", "read_only", "env"}'),
    ("outils: web jamais coupe chez Claude", "aihub/providers/__init__.py",
     "disallowed=() if web else claude_code.WEB_TOOLS", "disallowed=()"),
    ("outils: web jamais coupe chez Codex", "aihub/providers/codex.py",
     '            config["web_search"] = "disabled"', "            pass"),
    ("outils: serveur entier sans le separateur", "aihub/providers/claude_code.py",
     'tool_name.startswith(entry + "__")', "tool_name.startswith(entry)"),
    # --- delegation ---
    ("deleg: pas de limite", "aihub/delegate_mcp.py", "        if self.calls >= self.max_calls:\n", "        if False:\n"),
    ("deleg: modele hors liste", "aihub/delegate_mcp.py", "        if model not in self.models:\n", "        if False:\n"),
    ("deleg: l'autre IA peut ecrire", "aihub/delegate_mcp.py",
     'permission_mode="plan", model=providers.default_model', 'permission_mode="bypass", model=providers.default_model'),
    ("deleg: echec pris pour une reponse", "aihub/delegate_mcp.py",
     "        if error is not None:\n            raise Refused", "        if False:\n            raise Refused"),
    ("deleg: rien d'enregistre", "aihub/delegate_mcp.py",
     '                         ("ok" if error is None else "error", result.text', '                         ("running", result.text'),
    ("deleg: modele non propose par Codex", "aihub/tools.py",
     'if offered is None or model in offered or model in ("default", "codex"):', "if True:"),
    ("deleg: delai de Claude non leve", "aihub/tools.py",
     '        env["MCP_TOOL_TIMEOUT"] = str(DELEGATE_TIMEOUT_S * 1000)', "        pass"),
    ("deleg: env non transmis", "aihub/runner.py",
     "            env = {**(env if env is not None else claude_code.clean_env(os.environ)), **given.env}",
     "            pass"),
    ("deleg: cle de Codex donnee a Claude", "aihub/providers/claude_code.py",
     'if k in ("type", "command", "args", "env")}', "}"),
    # --- local AI ---
    ("local: lecture hors du dossier", "aihub/providers/ollama.py",
     "        if not target.is_relative_to(self.root):\n", "        if False:\n"),
    ("local: fichiers caches lus", "aihub/providers/ollama.py",
     '        if any(part.startswith(".") for part in target.relative_to(self.root).parts):\n            raise ValueError("fichier caché',
     '        if False:\n            raise ValueError("fichier caché'),
    ("local: outils sans fin", "aihub/providers/ollama.py", "            for _ in range(MAX_TOOL_ROUNDS + 1):\n",
     "            for _ in iter(int, 1):\n"),
    # « local: sans memoire » retired: since the import (11/10), a local AI's conversation with a reply
    # and no session is « imported » too, so the stateless test there is equivalent (measured: survives).
    ("local: plafond du cloud", "aihub/runner.py",
     "(s.rag_budget_local if local else s.rag_budget)", "s.rag_budget"),
    ("local: resume par defaut injoignable", "aihub/runner.py",
     "        return local[0] if local else (None if accounts.account_vars(env) else model)\n", "        return model\n"),
    ("local: vrai Ollama sous un faux Claude", "aihub/web/app.py",
     "ollama.PROVIDER: [ollama_url] if ollama_url else (None if command is None else False)}",
     "ollama.PROVIDER: [ollama_url] if ollama_url else None}"),
    ("local: reflexion activee", "aihub/providers/ollama.py", '"stream": True, "think": False}', '"stream": True}'),
    # --- search by meaning ---
    ("sens: tout passage proche", "aihub/memory.py",
     "keep = [h for h in hits if h[0] >= MEANING_MIN and h[0] >= best * MEANING_SPREAD][:limit]",
     "keep = hits[:limit]"),
    ("sens: sens ignore", "aihub/memory.py",
     "keys = semantic.fuse(word_keys, _by_meaning(conn, text, conversation_id))", "keys = word_keys"),
    ("sens: reponse en cours indexee", "aihub/semantic.py",
     "AND (m.role = 'user' OR m.status = 'ok')\n          AND NOT EXISTS", "\n          AND NOT EXISTS"),
    ("sens: question prise pour un document", "aihub/semantic.py",
     "        query = embed([str(text)], query=True)[0]\n", "        query = embed([str(text)])[0]\n"),
    ("sens: panne remontee", "aihub/semantic.py",
     "    except SemanticError as exc:\n        log.info(\"recherche par le sens indisponible", "    except ZeroDivisionError as exc:\n        log.info(\"recherche par le sens indisponible"),
    ("sens: vecteurs refaits", "aihub/semantic.py",
     "AND NOT EXISTS (SELECT 1 FROM memory_vectors v WHERE v.model = ? AND v.message_id = m.id)",
     "AND ? IS NOT NULL"),
    ("sens: recherche de la barre sans le sens", "aihub/memory.py",
     "    for key in _by_meaning(conn, text, limit=10, allowed_keys=eligible):\n", "    for key in []:\n"),
    # --- notifications ---
    ("notif: tout service accepte", "aihub/notify.py",
     '    if parts.scheme != "https" or not any(host == h or host.endswith("." + h) for h in PUSH_HOSTS):\n',
     '    if False:\n'),
    ("notif: suffixe sans point", "aihub/notify.py",
     'host == h or host.endswith("." + h)', 'host.endswith(h)'),
    ("notif: http accepte", "aihub/notify.py", 'parts.scheme != "https" or not any(', 'not any('),
    ("notif: alertes non choisies envoyees", "aihub/notify.py",
     '            if event not in json.loads(r["events_json"]):\n                continue\n',
     '            if False:\n                continue\n'),
    ("notif: abonnement perdu garde", "aihub/notify.py",
     "            if status in (404, 410):\n", "            if False:\n"),
    ("notif: echecs jamais purges", "aihub/notify.py",
     '                conn.execute("DELETE FROM push_subscriptions WHERE id = ? AND failures >= ?", (row["id"], MAX_FAILURES))\n',
     "                pass\n"),
    ("notif: succes ne remet pas a zero", "aihub/notify.py",
     'SET last_ok_at = ?, failures = 0 WHERE id = ?', 'SET last_ok_at = ? WHERE id = ?'),
    ("notif: erreur reseau remonte", "aihub/notify.py",
     "            except Exception as exc:  # network, refused keys: counted like a refusal\n",
     "            except ZeroDivisionError as exc:\n"),
    ("notif: cles non verifiees a l'abonnement", "aihub/notify.py",
     '    encrypt(b"", keys.get("p256dh", ""), keys.get("auth", ""))', "    pass"),
    ("notif: dernier enregistrement non marque", "aihub/notify.py",
     'plaintext + b"\\x02", None)', 'plaintext, None)'),
    ("notif: urgence de l'accord perdue", "aihub/notify.py",
     '"Urgency": "high" if event == "permission" else "normal"', '"Urgency": "normal"'),
    ("notif: ntfy dit le titre en minimal", "aihub/notify.py",
     'with_title=s.ntfy_detail == "titre")', "with_title=True)"),
    ("notif: ntfy sans filtre", "aihub/notify.py",
     "not (test or event in s.ntfy_events)", "not True"),
    ("notif: titre ntfy non encode", "aihub/notify.py", "    if text.isascii():\n", "    if True:\n"),
    ("notif: quota repete", "aihub/notify.py", "        if level > before:\n", "        if level:\n"),
    ("notif: ntfy en http distant", "aihub/notify.py",
     '(parts.scheme == "http" and not local)', '(False)'),
    ("notif: session non liee", "aihub/web/app.py",
     "notify.subscribe(conn, body.subscription, body.events, session_id=session_id,",
     "notify.subscribe(conn, body.subscription, body.events, session_id=None,"),
    ("notif: accord non signale", "aihub/web/jobs.py",
     '        self._alert("permission", job.conversation_id,\n', '        (lambda *a: None)("permission", job.conversation_id,\n'),
    ("notif: fin non signalee", "aihub/web/jobs.py", "                self._alert_end(job, done)\n", ""),
    ("notif: echec signale comme reussite", "aihub/web/jobs.py",
     '        elif status != "ok":\n            self._alert("error"', '        elif False:\n            self._alert("error"'),
    ("notif: essai filtre", "aihub/web/app.py", '"notification d\'essai", test=True,', '"notification d\'essai", test=False,'),
    ("runner: resume par un Claude non connecte", "aihub/runner.py",
     "    model = summary_model(model, commands=commands, env=env, workdir=workdir)\n", ""),
    ("runner: resume Codex sur un modele absent", "aihub/runner.py",
     "return next((m for m in LIGHT_CODEX if m in offered), \"codex\")", "return LIGHT_CODEX[0]"),
    # 1.3.0: the copies of the Codex app are searched for in aihub/locate.py.
    ("codex: copie la plus ancienne choisie", "aihub/locate.py",
     "    return max(copies, key=lambda c: (not _script(c.path), c.mtime, -c.index))\n",
     "    return min(copies, key=lambda c: (not _script(c.path), c.mtime, -c.index))\n"),
    ("codex: nouvelle place de l'appli ignoree", "aihub/locate.py",
     '            out += [("codex_app", local / "OpenAI" / "Codex" / "bin" if local else None, "*/codex.exe", False),\n',
     '            out += [("codex_app", None, "*/codex.exe", False),\n'),
    ("personnes: compte cree sur un nom pris", "aihub/auth.py",
     "        if conn.execute(\"SELECT 1 FROM users WHERE name = ?\", (name,)).fetchone():\n"
     "            raise AuthError(f\"le nom « {name} » est déjà pris\")\n"
     "        user_id = conn.execute(\"INSERT INTO users (name, password_hash, role, created_at) VALUES (?, ?, ?, ?)\",\n"
     "                               (name, stored, role,",
     "        user_id = conn.execute(\"INSERT INTO users (name, password_hash, role, created_at) VALUES (?, ?, ?, ?)\",\n"
     "                               (name, stored, role,"),
    ("personnes: renommage sur un nom pris", "aihub/auth.py",
     "        if taken is not None and taken[\"id\"] != user_id:", "        if False:"),
    ("personnes: renommage sur son propre nom refuse", "aihub/auth.py",
     " and taken[\"id\"] != user_id:", ":"),
    ("personnes: compte cree par une invitee", "aihub/web/app.py",
     "        access.require_admin(me())\n        if authority is None:\n            raise StoreError(\"ce serveur tourne sans comptes (--sans-mot-de-passe)\")\n        with database() as conn:\n            return {\"user\": auth.create_user(",
     "        if authority is None:\n            raise StoreError(\"ce serveur tourne sans comptes (--sans-mot-de-passe)\")\n        with database() as conn:\n            return {\"user\": auth.create_user("),
    ("import: messages caches de ChatGPT gardes", "aihub/importer.py",
     "        if (message.get(\"metadata\") or {}).get(\"is_visually_hidden_from_conversation\"):\n            continue\n", ""),
    ("import: outils et code de ChatGPT gardes", "aihub/importer.py",
     "if role not in (\"user\", \"assistant\") or content.get(\"content_type\") not in (\"text\", \"multimodal_text\"):",
     "if role not in (\"user\", \"assistant\", \"tool\"):"),
    ("import: autre branche de ChatGPT", "aihub/importer.py",
     "    chain = list(reversed(chain)) if chain else list(mapping.values())", "    chain = list(mapping.values())"),
    ("import: contexte injecte garde", "aihub/importer.py",
     "    return head.startswith(\"<\") or head.startswith(\"# AGENTS.md\") or head.startswith(\"Caveat:\")",
     "    return False"),
    ("import: sous-agent de Claude Code garde", "aihub/importer.py",
     " or row.get(\"isSidechain\") or row.get(\"isMeta\"):", ":"),
    ("import: session de sous-agent Codex gardee", "aihub/importer.py",
     "                return None  # a sub-agent's thread", "                pass  # a sub-agent's thread"),
    ("import: fusion sans en-tete", "aihub/importer.py",
     "            if merged and n == 0:\n                text = _header(item) + text\n", ""),
    ("import: suite sans la memoire importee", "aihub/runner.py",
     "                        and store.has_replies(self.conn, self.conversation_id))",
     "                        and False)"),
    ("import: une demande seule prise pour un import", "aihub/store.py",
     "WHERE conversation_id = ? AND role = 'assistant' AND status = 'ok' \"", "WHERE conversation_id = ? \""),
    ("import: lecture par une invitee", "aihub/web/app.py",
     "        access.require_owner(user)\n        return keep_reading(importer.local_sessions(local_home), user)",
     "        return keep_reading(importer.local_sessions(local_home), user)"),
    ("import: mode sans permission herite", "aihub/web/app.py",
     "permission_mode=body.permission_mode if body.permission_mode != \"bypass\" else \"manual\"",
     "permission_mode=body.permission_mode"),
    ("import: dossier de la session ignore", "aihub/web/app.py",
     "                    workdir = next((i.workdir for i in group if i.workdir and Path(i.workdir).is_dir()), \"\")",
     "                    workdir = \"\""),
    # Version 0.3: a server created from its page, optional passwords, roles.
    ("roles: compte sans mot de passe accepte tout", "aihub/auth.py",
     "        return password == \"\"  # an account without a password", "        return True  # an account without a password"),
    ("roles: mot de passe du serveur ignore", "aihub/auth.py",
     "            if lock is not None and not verify_password(server_password, lock[\"password_hash\"]):",
     "            if False:"),
    ("roles: serveur cree deux fois", "aihub/auth.py",
     "        if password_set(conn):\n            raise AuthError(\"ce serveur a déjà son propriétaire\")\n", ""),
    ("roles: invitation sans son role", "aihub/auth.py",
     "                                   (name, stored, row[\"role\"], db.utc_now())).lastrowid",
     "                                   (name, stored, \"invite\", db.utc_now())).lastrowid"),
    ("roles: invitation sans ses acces", "aihub/auth.py",
     "            _apply_shares(conn, user_id, json.loads(row[\"shares_json\"] or \"[]\"))\n", ""),
    ("roles: role du proprietaire change", "aihub/auth.py",
     "    if row[\"is_owner\"]:\n        raise AuthError(\"le propriétaire a tous les droits : son rôle ne change pas\")\n", ""),
    ("roles: serveur cree depuis ailleurs", "aihub/web/app.py",
     "        if not from_this_computer(request):\n            raise access.Forbidden(\"le serveur se crée depuis l'ordinateur où il tourne\")\n",
     ""),
    ("roles: creation du serveur sans en-tete", "aihub/web/app.py",
     "il tourne\")\n        if request.headers.get(CSRF_HEADER) != \"1\":  # a public path",
     "il tourne\")\n        if False:  # a public path"),
    ("roles: mot de passe du serveur par un autre", "aihub/web/app.py",
     "        access.require_owner(me())\n        with database() as conn:\n            auth.set_server_password(",
     "        with database() as conn:\n            auth.set_server_password("),
    ("roles: liste des personnes pour tous", "aihub/web/app.py",
     "    def users():\n        access.require_admin(me())", "    def users():\n        pass"),
    ("roles: membre dans le dossier de son choix", "aihub/web/app.py",
     "        if access.trusted(user):\n            workdir = body.workdir", "        if True:\n            workdir = body.workdir"),
    ('refresh-v10: roles: membre cree sans permission', 'aihub/web/app.py', "            if not profile['isolated']:\n                if body.isolation is not None:", '            if False:\n                if body.isolation is not None:'),
    ('refresh-v10: roles: membre passe sans permission', 'aihub/isolation.py', "        if old['isolated'] and not effective(conn, conv)['isolated']:", '        if False:'),
    ("roles: membre prend les outils du serveur", "aihub/web/app.py",
     "                if body.tools is not None and set(body.tools) - set(MEMBER_TOOLS):", "                if False:"),
    ("roles: membre ecrit dans le coffre", "aihub/web/app.py",
     "                if body.obsidian is not None:\n                    raise", "                if False:\n                    raise"),
    ("roles: conversation dans le projet d'un autre", "aihub/web/app.py",
     "        if body.project_id is not None and access.project_role(conn, user, body.project_id) != \"owner\":\n            raise access.Forbidden(\"une conversation se crée dans un projet à soi\")\n",
     ""),
    ("roles: commande de verification par un membre", "aihub/web/app.py",
     "            if body.check_command and not access.trusted(user):", "            if False:"),
    ("roles: lecture d'import d'un autre", "aihub/web/app.py",
     "        if reading is None or reading[1] != (user or {}).get(\"id\"):", "        if reading is None:"),
    ("roles: projet de membre avec un dossier", "aihub/web/app.py",
     "if body.workdir.strip() and access.trusted(user) else None", "if body.workdir.strip() else None"),
    ("refresh-v10: roles: projet d'un autre modifie", 'aihub/web/app.py', '            require_project(conn, user, pid)\n            if body.isolation is not None:\n', '            pass\n            if body.isolation is not None:\n'),
    ("roles: admin traite en invite", "aihub/access.py",
     "    return is_owner(user) or user.get(\"role\") == \"admin\"", "    return is_owner(user)"),
    ("roles: membre ne cree rien", "aihub/access.py",
     "    return sees_all(user) or user.get(\"role\") == \"membre\"", "    return sees_all(user)"),
    ("roles: sa propre conversation pas a lui", "aihub/access.py",
     "    if sees_all(user) or _owns(user, conversation):", "    if sees_all(user):"),
    ("roles: ses conversations invisibles", "aihub/access.py",
     "        \"UNION SELECT id FROM conversations WHERE owner_id = ? \"", "        \"UNION SELECT id FROM conversations WHERE owner_id = ? AND 0 \""),
    ("roles: membre partage ce qui n'est pas a lui", "aihub/access.py",
     "    if not mine:\n        raise Forbidden(\"seul celui qui l'a créé (ou un admin) le partage\")", "    if False:\n        raise Forbidden(\"seul celui qui l'a créé (ou un admin) le partage\")"),
    ("roles: partage a un admin", "aihub/access.py",
     "    if person[\"is_owner\"] or person[\"role\"] == \"admin\":", "    if person[\"is_owner\"]:"),
    ("roles: alertes sans les admins", "aihub/notify.py",
     " or r[\"is_owner\"] or r[\"role\"] == \"admin\" or ", " or r[\"is_owner\"] or "),
    ("roles: alertes sans le membre proprietaire", "aihub/notify.py",
     "        if project[\"owner_id\"] is not None:\n            found.add(project[\"owner_id\"])\n", ""),
    ("refresh-v10: roles: comptes de l'ordinateur deconnectes d'ici", 'aihub/accounts.py', '        if self.execution is None and not own_accounts(user):\n            # Claude Code and Codex use them outside the hub too', '        if False:\n            # Claude Code and Codex use them outside the hub too'),
    ("roles: comptes de deux bases melanges", "aihub/web/jobs.py",
     "    return path.with_name(f\"{path.stem}-{kind}\")", "    return path.with_name(kind)"),
    # Version 0.5: regenerate, branch, compare, instructions, dashboard.
    ("v05: regenerer en lecture seule", "aihub/web/app.py",
     "            conv = access.require(conn, user, cid, \"write\")\n            if isolation.suspended(conn, cid):\n                raise StoreError('accès suspendus : réactivez cet espace avant de régénérer une réponse')\n            if conv[\"mode\"] != \"single\":\n                raise StoreError(\"on régénère",
     "            conv = access.require(conn, user, cid, \"read\")\n            if isolation.suspended(conn, cid):\n                raise StoreError('accès suspendus : réactivez cet espace avant de régénérer une réponse')\n            if conv[\"mode\"] != \"single\":\n                raise StoreError(\"on régénère"),
    ("v05: IA changee par un invite", "aihub/web/app.py",
     "                if access.conversation_role(conn, user, conv) != \"owner\":\n                    raise access.Forbidden(\"seul celui qui possède la conversation change son IA\")",
     "                if False:\n                    raise access.Forbidden(\"seul celui qui possède la conversation change son IA\")"),
    ("v05: fichiers perdus a la regeneration", "aihub/web/app.py",
     "            conn.execute(\"UPDATE attachments SET message_id = NULL, user_id = ? WHERE message_id = ?\",\n"
     "                         ((user or {}).get(\"id\"), request[\"id\"]))\n", ""),
    ("v05: regenerer sans reponse a la fin", "aihub/store.py",
     "    if request[\"role\"] != \"user\" or reply[\"role\"] != \"assistant\" or reply[\"status\"] in (\"pending\", \"running\"):",
     "    if reply[\"status\"] in (\"pending\", \"running\"):"),
    ("v05: branche par un invite", "aihub/web/app.py",
     "        user = me()\n        access.require_creator(user)\n        with database() as conn:\n            message = store.get_message(conn, mid)",
     "        user = me()\n        with database() as conn:\n            message = store.get_message(conn, mid)"),
    ("v05: branche avec la demande corrigee", "aihub/store.py",
     "\"duration_ms FROM messages WHERE conversation_id = ? AND id < ? AND content <> '' \"",
     "\"duration_ms FROM messages WHERE conversation_id = ? AND id <= ? AND content <> '' \""),
    ("v05: copies comptees deux fois", "aihub/store.py",
     "                (target_id, r[\"role\"], None, r[\"model\"],", "                (target_id, r[\"role\"], r[\"provider\"], r[\"model\"],"),
    ("v05: branche depuis une reponse", "aihub/web/app.py",
     "            if message[\"role\"] != \"user\":\n                raise StoreError(\"on modifie une demande, pas une réponse\")\n", ""),
    ("v05: comparaison qui ecrit", "aihub/compare.py",
     "                    workdir=conversation[\"workdir\"], permission_mode=\"plan\",", "                    workdir=conversation[\"workdir\"], permission_mode=\"manual\","),
    ("v05: garder sans oublier les sessions", "aihub/compare.py",
     "        conn.execute(\"UPDATE messages SET session_id = NULL WHERE conversation_id = ?\", (variant[\"conversation_id\"],))\n", ""),
    ("v05: comparer avec une IA non proposee", "aihub/web/app.py",
     "            if body.compare_with not in offered_models(current()):", "            if False:"),
    ("v05: garder en lecture seule", "aihub/web/app.py",
     "            access.require(conn, me(), row[\"conversation_id\"], \"write\")\n            if jobs.running(row[\"conversation_id\"]):",
     "            access.require(conn, me(), row[\"conversation_id\"], \"read\")\n            if jobs.running(row[\"conversation_id\"]):"),
    ("v05: comparaison gratuite", "aihub/usage.py",
     "    for query in (_REPLIES, _SUBTASKS, _OPINIONS, _VARIANTS):", "    for query in (_REPLIES, _SUBTASKS, _OPINIONS):"),
    ("v05: consignes du projet oubliees", "aihub/store.py",
     "            parts.append(f\"Pour tout le projet :\\n{project['instructions']}\")", "            pass"),
    ("v05: consignes sans limite", "aihub/store.py",
     "    if len(text) > INSTRUCTIONS_MAX:", "    if False:"),
    ("v05: consignes pas envoyees", "aihub/runner.py",
     "            instructions=store.join_instructions(store.person_instructions(self.conn, conversation),",
     "            instructions=store.join_instructions(None,"),
    ("v05: tableau de bord pour tous", "aihub/web/app.py",
     "        access.require_owner(me())\n        data_dir = Path(db_path).resolve().parent", "        data_dir = Path(db_path).resolve().parent"),
    ("v05: tout le journal", "aihub/web/app.py",
     "                log = [line.rstrip(\"\\n\") for line in fh.readlines()[-50:]]",
     "                log = [line.rstrip(\"\\n\") for line in fh.readlines()]"),
    # Favorites, export, consumption, files and images, the one-click update.
    ("extras: favori d'un autre retire", "aihub/store.py",
     "    if conn.execute(\"DELETE FROM favorites WHERE id = ? AND user_id IS ?\", (favorite_id, user_id)).rowcount == 0:",
     "    if conn.execute(\"DELETE FROM favorites WHERE id = ? OR ? IS NULL\", (favorite_id, user_id)).rowcount == 0:"),
    ("extras: favoris sans limite", "aihub/store.py",
     "    if count >= FAVORITES_MAX:", "    if False:"),
    ("extras: export d'une conversation non vue", "aihub/web/app.py",
     "            conv = dict(access.require(conn, me(), cid))\n            names = {u[\"id\"]: u[\"name\"] for u in auth.list_users(conn)}\n            messages = [dict(m) for m in store.list_messages(conn, cid)]\n        author",
     "            conv = dict(store.get_conversation(conn, cid))\n            names = {u[\"id\"]: u[\"name\"] for u in auth.list_users(conn)}\n            messages = [dict(m) for m in store.list_messages(conn, cid)]\n        author"),
    ("extras: page imprimee non echappee", "aihub/web/app.py",
     "        page = (f'<!doctype html><html lang=\"fr\"><meta charset=\"utf-8\"><title>{html.escape(conv[\"title\"])}</title>'",
     "        page = (f'<!doctype html><html lang=\"fr\"><meta charset=\"utf-8\"><title>{conv[\"title\"]}</title>'"),
    ("extras: consommation pour tous", "aihub/web/app.py",
     "        access.require_admin(me())\n        if days not in usage.PERIODS:", "        if days not in usage.PERIODS:"),
    ("extras: import compte dans la consommation", "aihub/usage.py",
     "WHERE a.role = 'assistant' AND a.provider IS NOT NULL AND a.created_at >= ?",
     "WHERE a.role = 'assistant' AND a.created_at >= ?"),
    ("extras: reponse comptee au mauvais auteur", "aihub/usage.py",
     "SELECT (SELECT u.user_id FROM messages u WHERE u.conversation_id = a.conversation_id AND u.id < a.id\n        AND u.role = 'user' ORDER BY u.id DESC LIMIT 1) AS user_id,",
     "SELECT NULL AS user_id,"),
    ("pieces: fichier d'un autre envoye", "aihub/attachments.py",
     "        row = conn.execute(\"SELECT * FROM attachments WHERE id = ? AND conversation_id = ? AND user_id IS ? \"\n"
     "                           \"AND message_id IS NULL\", (aid, conversation_id, user_id)).fetchone()",
     "        row = conn.execute(\"SELECT * FROM attachments WHERE id = ? AND conversation_id = ? AND ? IS ? \"\n"
     "                           \"AND message_id IS NULL\", (aid, conversation_id, user_id, user_id)).fetchone()"),
    ("pieces: fichier envoye deux fois", "aihub/attachments.py",
     "                           \"AND message_id IS NULL\", (aid, conversation_id, user_id)).fetchone()",
     "                           \"AND 1\", (aid, conversation_id, user_id)).fetchone()"),
    ("pieces: trop de fichiers", "aihub/attachments.py",
     "    if len(ids) > MAX_PER_REQUEST:", "    if False:"),
    ("pieces: tout affiche dans la page", "aihub/web/app.py",
     "        disposition = \"inline\" if image else \"attachment\"", "        disposition = \"inline\""),
    ("pieces: type du fichier cru", "aihub/web/app.py",
     "        return FileResponse(row[\"stored\"], media_type=row[\"mime\"] if image else \"application/octet-stream\",",
     "        return FileResponse(row[\"stored\"], media_type=row[\"mime\"],"),
    ("pieces: fichiers gardes apres suppression", "aihub/web/app.py",
     "        attachments.forget_conversation(pieces, cid)  # its files go with it\n", ""),
    ("pieces: envoi en lecture seule", "aihub/web/app.py",
     "        with database() as conn:\n            access.require(conn, user, cid, \"write\")\n        data = await request.body()",
     "        with database() as conn:\n            access.require(conn, user, cid, \"read\")\n        data = await request.body()"),
    ("pieces: fichier vu sans la conversation", "aihub/web/app.py",
     "            row = attachments.get(conn, aid)\n            access.require(conn, me(), row[\"conversation_id\"])",
     "            row = attachments.get(conn, aid)"),
    ("pieces: Claude Code sans le dossier", "aihub/providers/__init__.py",
     "        extra += [\"--add-dir\", folder]", "        pass"),
    ("pieces: Codex sans l'image", "aihub/providers/codex.py",
     "                        + [{\"type\": \"localImage\", \"path\": p} for p in self.images]}", "}"),
    ("pieces: IA locale sans l'image", "aihub/providers/ollama.py",
     "        if self.images:\n            messages[1][\"images\"]", "        if False:\n            messages[1][\"images\"]"),
    ("pieces: texte du fichier absent", "aihub/attachments.py",
     "        if text is not None:\n            fence", "        if False:\n            fence"),
    ("pieces: dossier dans le nom", "aihub/attachments.py",
     "    name = str(name or \"\").replace(\"\\\\\", \"/\").rsplit(\"/\", 1)[-1]", "    name = str(name or \"\")"),
    ('refresh-v10: maj: fichier qui ecrit hors de son dossier', 'aihub/updater.py', 'not n.startswith(folder)', 'False'),
    ("maj: version annoncee non verifiee", "aihub/updater.py",
     "    if not found or found.group(1) != version:", "    if not found:"),
    ("maj: telechargement d'ailleurs", "aihub/updater.py",
     "    if not url.startswith(f\"https://github.com/{REPO}/releases/download/\"):", "    if False:"),
    ("maj: copie git mise a jour", "aihub/updater.py",
     "    reason = why_not_here(root)\n    if reason:\n        raise UpdateError(reason)", "    reason = why_not_here(root)"),
    ("maj: pendant qu'une IA travaille", "aihub/web/app.py",
     "        if jobs.busy():\n            raise Busy(\"une IA travaille : attendez qu'elle ait fini avant de mettre",
     "        if False:\n            raise Busy(\"une IA travaille : attendez qu'elle ait fini avant de mettre"),
    ("maj: lancee par un invite", "aihub/web/app.py",
     "        access.require_owner(me())\n        if jobs.busy():\n            raise Busy(\"une IA travaille : attendez qu'elle ait fini avant de mettre à jour\")",
     "        if jobs.busy():\n            raise Busy(\"une IA travaille : attendez qu'elle ait fini avant de mettre à jour\")"),
    ('refresh-v10: maj: echec sans retour en arriere', 'aihub/updater.py', '            shutil.move(str(backup / name), str(root / name))\n', '            pass\n'),
    ('refresh-v10: maj: serveur pas relance', 'aihub/updater.py', '            process = launch(probe)\n', '            process = None\n'),
    ("maj: version egale prise pour plus recente", "aihub/updater.py",
     "            if key is None or key <= (version_key(current) or (0,)):", "            if key is None or key < (version_key(current) or (0,)):"),
    # A device connected by its QR code, devices named.
    ("appareils: lien deja utilise accepte", "aihub/auth.py",
     "        if conn.execute(\"UPDATE device_links SET used_at = ? WHERE id = ? AND used_at IS NULL\",",
     "        if 1 + 0 * conn.execute(\"UPDATE device_links SET used_at = ? WHERE id = ? AND used_at IS NULL\","),
    ("appareils: lien d'une personne desactivee", "aihub/auth.py",
     "        if row is None or row[\"disabled\"] or _parse(", "        if row is None or _parse("),
    ("appareils: lien expire accepte", "aihub/auth.py",
     " or _parse(row[\"expires_at\"]) <= self.now():\n            self._record_failure(conn, client)\n            raise AuthError(refused)",
     ":\n            self._record_failure(conn, client)\n            raise AuthError(refused)"),
    ("appareils: appareil sans son nom", "aihub/auth.py",
     "        conn.execute(\"UPDATE sessions SET label = ? WHERE token_hash = ?\", (row[\"label\"], _token_hash(token)))\n", ""),
    ("appareils: renommer l'appareil d'un autre", "aihub/auth.py",
     "        return conn.execute(\"UPDATE sessions SET label = ? WHERE id = ? AND user_id = \"\n"
     "                            \"(SELECT user_id FROM sessions WHERE token_hash = ?)\",",
     "        return conn.execute(\"UPDATE sessions SET label = ? WHERE id = ? OR ? IS NULL\",\n"
     "                            "),
    ("appareils: lien pour le compte d'un autre", "aihub/web/app.py",
     "        if not (access.sees_all(user) or (user and user[\"id\"] == uid)):", "        if False:"),
    ("appareils: acceptation sans en-tete", "aihub/web/app.py",
     "        if request.headers.get(CSRF_HEADER) != \"1\":  # a public path: the guard does not check it here\n"
     "            raise access.Forbidden(\"requête refusée : en-tête X-AIHub manquant\")\n        with database() as conn:\n"
     "            token = authority.accept_device_link(",
     "        with database() as conn:\n            token = authority.accept_device_link("),
    ("appareils: lien en 127.0.0.1", "aihub/web/app.py",
     "            name = tailscale_name(request.url.port)\n            if name:", "            name = None\n            if name:"),
    ("appareils: relais d'un autre port", "aihub/web/app.py",
     "    return relays.get(port)", "    return next(iter(relays.values()), None)"),
    # Version 0.2: each person's own AI accounts (aihub/accounts.py).
    ("comptes: le proprietaire dans un dossier vide", "aihub/accounts.py",
     '    return bool(user) and not user.get("is_owner")', "    return bool(user)"),
    ("comptes: une invitee sur les comptes du PC", "aihub/accounts.py",
     "    if not own_accounts(user):\n        return base\n    env = dict(", "    if True:\n        return base\n    env = dict("),
    ("comptes: cle de l'ordinateur laissee", "aihub/accounts.py",
     "        env.pop(name, None)\n", "        pass\n"),
    ("comptes: dossier Claude oublie", "aihub/accounts.py",
     '    env["CLAUDE_CONFIG_DIR"] = str(found[claude_code.PROVIDER])\n', ""),
    ("comptes: dossier Codex oublie", "aihub/accounts.py",
     '    env["CODEX_HOME"] = str(found[codex.PROVIDER])\n', ""),
    ("comptes: delegation sur le compte du PC", "aihub/tools.py",
     '            servers[DELEGATE]["env"] = dict(account_env)\n', "            pass\n"),
    ("comptes: le runner ne transmet pas le compte a la delegation", "aihub/runner.py",
     "project=project, account_env=accounts.account_vars(self.env))", "project=project)"),
    ("comptes: requete sur le compte du PC", "aihub/web/jobs.py",
     '"command": self.command, "env": self.env_of(job),', '"command": self.command, "env": self.env,'),
    ("comptes: plan sur le compte du PC", "aihub/web/jobs.py",
     "self.db_path, plan_id, command=self.command, env=self.env_of(job),",
     "self.db_path, plan_id, command=self.command, env=self.env,"),
    ("comptes: extensions Codex lues dans le dossier du PC", "aihub/providers/codex.py",
     '            home = Path(env["CODEX_HOME"])  # a person', '            home = None  # a person'),
    ("comptes: quota d'une invitee range au PC", "aihub/runner.py",
     "            user_id=self.account_id,\n", ""),
    ("comptes: quota lu sans la personne", "aihub/store.py",
     "WHERE provider = ? AND user_id IS ? ORDER BY id DESC LIMIT 1\",\n        (provider, user_id),",
     "WHERE provider = ? ORDER BY id DESC LIMIT 1\",\n        (provider,),"),
    ("comptes: quota rafraichi range au PC", "aihub/web/app.py",
     "windows_json=json.dumps(info.windows), user_id=account)", "windows_json=json.dumps(info.windows))"),
    ("comptes: quota rafraichi sur le compte du PC", "aihub/web/app.py",
     "                                env=run_env, warning_from", "                                env=env, warning_from"),
    ("comptes: alerte d'une invitee chez tous", "aihub/notify.py",
     '                if r["user_id"] == user_id:  # about', '                if True:  # about'),
    ("comptes: alerte d'une invitee par ntfy", "aihub/notify.py",
     "if (not test or session_id is None) and user_id is None:", "if (not test or session_id is None):"),
    ("comptes: niveaux d'alerte melanges", "aihub/notify.py",
     "            before = self._quota_levels.get((provider_name, user_id), 0)",
     "            before = self._quota_levels.get((provider_name, None), 0)"),
    ("comptes: dossiers gardes apres suppression", "aihub/web/app.py",
     "        people_accounts.forget(uid)  # their", "        pass  # their"),
    ("comptes: navigateur ouvert sur le serveur", "aihub/accounts.py",
     'env = dict(env, BROWSER="none")', "env = dict(env)"),
    ("comptes: code Claude non verifie", "aihub/accounts.py",
     "            ok = proc.returncode == 0 and claude_signed_in(", "            ok = True or claude_signed_in("),
    ("comptes: refus Codex pris pour un succes", "aihub/accounts.py",
     "                        if params.get(\"success\"):", "                        if True:"),
    ("comptes: code colle sans controle", "aihub/accounts.py",
     "        if not code or len(code) > 512 or any(c.isspace() for c in code):", "        if False:"),
    ("comptes: connexion jamais abandonnee", "aihub/accounts.py",
     "time.monotonic() - login.started > LOGIN_TIMEOUT_S:", "False:"),
    ('refresh-v10: comptes: annulation sans arret', 'aihub/accounts.py', '                finally:\n                    claude_code._kill_tree(proc)\n', '                finally:\n                    pass\n'),
    # « comptes: proprietaire connecte dans un dossier » retired (11/10): the owner now signs in the
    # computer's accounts from the hub (a new server); never signs them out (« roles: comptes de
    # l'ordinateur deconnectes d'ici »).
]

MUTATIONS += [
    ('refresh-v10: v051: resume sur le compte serveur', 'aihub/web/app.py', 's.summary_model, env=people_accounts.env_for(me()),', 's.summary_model, env=env,'),
    ("v051: variantes laissees actives", "aihub/store.py",
     '        conn.execute("UPDATE variants SET status = \'stopped\', error = ? WHERE status = \'running\'", (INTERRUPTED,))\n', ''),
    ('refresh-v10: v051: consignes perdues dans la branche', 'aihub/web/app.py', '(conv["instructions"], \'[]\' if policy[\'isolated\'] else', "(None, '[]' if policy['isolated'] else"),
    ("v051: branche sans fichiers", "aihub/web/app.py",
     '                    files = attachments.copy_for_branch(conn, pieces, message["conversation_id"], cid, mid,\n'
     '                                                        copied, (user or {}).get("id"))',
     '                    files = []'),
    ("v051: local non propose", "aihub/routing.py",
     '        if ("ollama", model) not in excluded:', '        if False:'),
    ("v06: epingles melangees", "aihub/library.py",
     'SELECT * FROM conversation_preferences WHERE user_id IS ?', 'SELECT * FROM conversation_preferences WHERE ? IS NOT NULL'),
    ("v06: recherche sans les droits", "aihub/memory.py",
     '    if conversation_ids is not None:\n', '    if False:\n'),
    ("v06: modele personnel visible a tous", "aihub/library.py",
     'SELECT * FROM work_templates WHERE user_id IS ? ORDER BY title COLLATE NOCASE',
     'SELECT * FROM work_templates WHERE ? IS NOT NULL ORDER BY title COLLATE NOCASE'),
    ("v06: suppression du modele d'un autre", "aihub/library.py",
     'DELETE FROM work_templates WHERE id = ? AND user_id IS ?', 'DELETE FROM work_templates WHERE id = ? OR user_id IS ?'),
    ("v06: champs non remplis acceptes", "aihub/library.py",
     '    if any(not str(values.get(name) or "").strip() for name in required):', '    if False:'),
    ("v06: protocole de lien non filtre", "aihub/web/static/markdown.js",
     'safeUrl(url)\n      ?', 'true\n      ?'),
]


def _kill_tree(proc):
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                       capture_output=True, creationflags=NO_WINDOW)
    else:
        proc.kill()
    proc.wait()


TIMEOUT = 900
MUTATIONS += [('v10: untrusted host execution', 'aihub/isolation.py', "if not policy['isolated'] and not access.trusted(dict(person)):", 'if False:'), ('v10: invalid backup published', 'aihub/backup.py', '        count = restore_assets(chosen, staged_db)\n', '        count = 0\n')]

MUTATIONS.append(('v10: deleted account VM reused', 'aihub/isolation.py', "        context.account_id = f'{account_id}:{nonce}'\n", '        context.account_id = account_id\n'))

MUTATIONS += [('v10: stale account response cached', 'aihub/web/static/sw.js', 'epoch && epoch === requestedEpoch', 'epoch'), ('v10: failed migration DB retained', 'aihub/updater.py', '            copy_database(saved_db, database)\n', '            pass\n')]

MUTATIONS.append(('v10: recreated conversation space reused', 'aihub/isolation.py', "    context.scope_nonce = conn.execute('SELECT nonce FROM sandbox_contexts WHERE conversation_id = ?',\n                                       (conversation['id'],)).fetchone()[0]\n", '    context.scope_nonce = None\n'))

MUTATIONS.extend([
    ('v101: emergency suspension not saved', 'aihub/web/app.py',
     '                isolation.suspend(conn, cid)\n', '                pass\n'),
    ('v101: suspended execution accepted', 'aihub/isolation.py',
     "    if policy.get('paused'):\n", '    if False:\n'),
])

MUTATIONS.extend([
    ('v102: overwrite accepted', 'aihub/filesystem.py',
     'file_fd = os.open(walk, os.O_CREAT | os.O_EXCL | os.O_WRONLY', 'file_fd = os.open(walk, os.O_CREAT | os.O_WRONLY'),
    ('v102: allowed roots ignored', 'aihub/filesystem.py',
     '    if allowed and not any(p.is_relative_to(r) for r in allowed):', '    if False:'),
    ('v102: untrusted server file creation accepted', 'aihub/web/app.py',
     '        if not access.trusted(user):\n            raise access.Forbidden(\'la création sur le PC serveur',
     '        if False:\n            raise access.Forbidden(\'la création sur le PC serveur'),
    ('v102: junction creation accepted', 'aihub/filesystem.py',
     '        if info.attributes & 0x400 or not info.attributes & 0x10:',
     '        if not info.attributes & 0x10:'),
])

MUTATIONS.append(('v108: finished tasks replayed', 'aihub/store.py',
    "UPDATE subtasks SET status = 'pending', finished_at = NULL WHERE plan_id = ? AND status <> 'ok'",
    "UPDATE subtasks SET status = 'pending', finished_at = NULL WHERE plan_id = ?"))
MUTATIONS.append(('v108: another account session resumed', 'aihub/runner.py',
    "source['session_id'] and source['session_scope'] == self.session_scope", "source['session_id']"))
MUTATIONS.append(('v108: subtask checkpoint not persisted', 'aihub/orchestrator.py',
    '                journal.execute("UPDATE subtasks SET checkpoint = substr(checkpoint || ?, -200000) WHERE id = ? AND status = \'running\'",\n                                (chunk, subtask[\'id\']))',
    '                pass'))
MUTATIONS.append(('v106: project documents without project access', 'aihub/web/app.py',
    "and access.reads_project(conn, (user or {}).get('id'), conversation)",
    'and True'))
MUTATIONS.append(('v106: own isolated memory reads project documents', 'aihub/web/app.py',
    "and (not policy['isolated'] or policy['memory'] == 'project')", 'and True'))
MUTATIONS.append(('v106: project context ignores its scope', 'aihub/rag.py',
    "    if not project or conversation['project_id'] is None:",
    "    if conversation['project_id'] is None:"))
MUTATIONS.append(('v103: cancellation not broadcast', 'aihub/web/app.py',
    '        jobs.hub.publish(stopped["conversation_id"], {"type": "plan_updated", "plan_id": pid})\n',
    '        # cancellation not broadcast\n'))
MUTATIONS.append(('v103: cancellation waits for refresh', 'aihub/web/static/app.js',
    '      state.data.plans = state.data.plans.map(plan => plan.id === result.plan.id ? result.plan : plan);',
    '      // cancelled plan remains proposed locally'))
MUTATIONS.append(('v103: rejected plan remains busy', 'aihub/providers/claude_code.py',
                 "            self.stop(reason='plan_rejected')\n", '            pass\n'))

MUTATIONS.append(('v109: reply noted twice', 'aihub/web/app.py',
                 '            if message["noted_at"]:\n                raise StoreError("cette réponse est déjà dans la note")\n', ''))
MUTATIONS.append(('v109: synthesis noted by hand', 'aihub/web/app.py',
                 '            if conn.execute("SELECT 1 FROM plans WHERE synthesis_message_id = ?", (mid,)).fetchone():\n', '            if False:\n'))
MUTATIONS.append(('v109: rewrite copies the synthesis twice', 'aihub/web/jobs.py',
                 'if message["noted_at"] and message["id"] not in in_plans:', 'if message["noted_at"]:'))
MUTATIONS.append(('v109: noted plan leaves its synthesis unmarked', 'aihub/web/jobs.py',
                 '                store.mark_noted(conn, plan["synthesis_message_id"])\n', '                pass\n'))
MUTATIONS.append(('v109: automatic request passed as is', 'aihub/orchestrator.py',
                 "    if str(request or '').strip() == AUTO_PLAN_REQUEST:\n", '    if False:\n'))
MUTATIONS.append(('v109: synthesis not told of the validation', 'aihub/orchestrator.py',
                 '{VALIDATED} Ne présente pas son exécution comme un écart à la demande.', ''))
MUTATIONS.append(('v109: subtask not told of the validation', 'aihub/orchestrator.py',
                 "Plan d'ensemble : {plan.get('summary') or 'non précisé'}\n{VALIDATED}\n\nTa sous-tâche", "Plan d'ensemble : {plan.get('summary') or 'non précisé'}\n\nTa sous-tâche"))
MUTATIONS.append(('v109: anyone lists the vaults', 'aihub/web/app.py',
                 '    def list_vaults(refresh: bool = False):\n        access.require_owner(me())\n', '    def list_vaults(refresh: bool = False):\n'))
MUTATIONS.append(("v109: anyone lists a vault's notes", 'aihub/web/app.py',
                 '    def vault_notes(vault: str):\n        access.require_owner(me())\n', '    def vault_notes(vault: str):\n'))
MUTATIONS.append(('v109: anyone reads a note', 'aihub/web/app.py',
                 '    def vault_note(vault: str, path: str):\n        access.require_owner(me())\n', '    def vault_note(vault: str, path: str):\n'))
MUTATIONS.append(('v109: created vault not remembered', 'aihub/web/app.py',
                 '        vaults.record_created(vaults.record_file(db_path), result["path"])\n', ''))
MUTATIONS.append(('v109: any folder opens as a vault', 'aihub/vaults.py',
                 '    if not (is_vault(path) or settings):\n', '    if not Path(path).is_dir():\n'))
MUTATIONS.append(('v109: settings folder refused without .obsidian', 'aihub/vaults.py',
                 '        exists = is_vault(path) or ((active or entry["projects"]) and Path(path).is_dir())\n', '        exists = is_vault(path)\n'))
MUTATIONS.append(('v109: hidden folder of a vault read', 'aihub/vaults.py',
                 '    if any(part.startswith(".") for part in Path(str(note)).parts):\n', '    if False:\n'))
MUTATIONS.append(('v109: walk enters hidden folders', 'aihub/vaults.py',
                 'e.is_dir(follow_symlinks=False) and not e.name.startswith((".", "$", "~"))', 'e.is_dir(follow_symlinks=False)'))
MUTATIONS.append(('v109: walk goes past its depth', 'aihub/vaults.py',
                 '        if depth <= 0:\n            continue\n', ''))
MUTATIONS.append(('v109: walk never stops', 'aihub/vaults.py',
                 '        if looked >= max_dirs or time.monotonic() > deadline:\n', '        if False:\n'))
MUTATIONS.append(('v109: hidden notes counted', 'aihub/vaults.py',
                 '    for folder, dirs, files in os.walk(vault):\n        dirs[:] = [d for d in dirs if not d.startswith(".")]\n', '    for folder, dirs, files in os.walk(vault):\n'))
MUTATIONS.append(('v109: hidden notes listed', 'aihub/vaults.py',
                 '        dirs[:] = sorted(d for d in dirs if not d.startswith("."))\n', '        dirs[:] = sorted(dirs)\n'))
MUTATIONS.append(('v109: notes listed without end', 'aihub/vaults.py',
                 '            if len(notes) >= MAX_NOTES:\n', '            if False:\n'))
MUTATIONS.append(('v109: long note read whole', 'aihub/vaults.py',
                 '        raw = fh.read(MAX_NOTE_BYTES)\n', '        raw = fh.read()\n'))
MUTATIONS.append(('v109: known vault said found on the disk', 'aihub/vaults.py',
                 '        if _key(path) not in entries:  # "found on the disk": known by nothing else\n', '        if True:\n'))
MUTATIONS.append(('v109: active vault not first', 'aihub/vaults.py',
                 'vaults.sort(key=lambda v: (not v["active"], not v["exists"], v["name"].lower(), v["path"].lower()))', 'vaults.sort(key=lambda v: (v["name"].lower(), v["path"].lower()))'))
MUTATIONS.append(('v109: a broken Obsidian list breaks the search', 'aihub/vaults.py',
                 '        except (OSError, ValueError):\n            continue\n        for entry in (data.get("vaults") or {}).values():\n', '        except OSError:\n            continue\n        for entry in (data.get("vaults") or {}).values():\n'))
MUTATIONS.append(('v109: created vault listed twice', 'aihub/vaults.py',
                 '        known = [v for v in created(path_of_record) if _key(v["path"]) != _key(vault_path)]\n', '        known = list(created(path_of_record))\n'))

MUTATIONS.append(('v110: guest browses the PC', 'aihub/web/app.py',
                 "        if not access.trusted(user):\n            raise access.Forbidden('parcourir le PC serveur est réservé au propriétaire et aux administrateurs')\n        return browse.folders(path, current().allowed_roots, files=kinds)\n", '        return browse.folders(path, current().allowed_roots, files=kinds)\n'))
MUTATIONS.append(("v110: admin chooses the owner's settings", 'aihub/web/app.py',
                 "        if scope == 'settings':\n            # The owner chooses a setting (allowed folders, vault, backups): anywhere on the PC's local disks.\n            access.require_owner(user)\n", "        if scope == 'settings':\n            # The owner chooses a setting (allowed folders, vault, backups): anywhere on the PC's local disks.\n            access.trusted(user) or access.require_owner(user)\n"))
MUTATIONS.append(('v110: working folder browsed past the allowed folders', 'aihub/web/app.py',
                 '        return browse.folders(path, current().allowed_roots, files=kinds)\n', '        return browse.folders(path, files=kinds)\n'))
MUTATIONS.append(("v110: a shared writer reads the conversation's files", 'aihub/web/app.py',
                 "                conv = access.require(conn, user, conversation_id, 'owner')\n            if not conv['workdir']:", "                conv = access.require(conn, user, conversation_id, 'read')\n            if not conv['workdir']:"))
MUTATIONS.append(('v110: guest lists a working folder', 'aihub/web/app.py',
                 "        if not access.trusted(user):\n            raise access.Forbidden('parcourir le PC serveur est réservé au propriétaire et aux administrateurs')\n        if not base:\n", '        if not base:\n'))
MUTATIONS.append(('v110: any base folder listed', 'aihub/web/app.py',
                 '        return browse.entries(check_workdir(base), path)\n', '        return browse.entries(base, path)\n'))
MUTATIONS.append(('v110: outside the allowed folders shown', 'aihub/browse.py',
                 '    here = rule.folder(path)  # local, allowed, existing: or said why not\n', '    here = places.Places().folder(path)\n'))
MUTATIONS.append(('v110: a link out of the allowed folders shown', 'aihub/browse.py',
                 '        if allowed and not _inside(target, allowed):\n            continue  # a link out of the allowed folders\n', ''))
MUTATIONS.append(('v110: a way up out of the allowed folders', 'aihub/browse.py',
                 '    if parent is not None and allowed and not _inside(parent, allowed):\n        parent = None\n', ''))
MUTATIONS.append(('v110: hidden folders shown', 'aihub/browse.py',
                 '                if not e.name.startswith((".", "$")):\n                    items.append(e)\n', '                items.append(e)\n'))
MUTATIONS.append(('v110: files shown as folders', 'aihub/browse.py',
                 '            elif files is not None and entry.is_file() and (files == "*" or Path(entry.name).suffix.lower() in files):\n',
                 '            elif entry.is_file():\n'))
MUTATIONS.append(('v110: the allowed folders are not where it starts', 'aihub/browse.py',
                 '    if roots:\n        return [{"name": str(r), "path": str(r)} for r in roots if r.is_dir()]\n', ''))
# v110: "a relative path taken" retired: on Windows browse.local_path refuses a relative
# path first, so the is_absolute() check only acts on Linux (test_bad_paths, run there).
MUTATIONS.append(('v110: a file opened as a folder', 'aihub/places.py',
                 '        if not path.is_dir():\n            raise StoreError(f"ce {what} n\'existe pas : {path}")\n        return path\n\n    def file(',
                 '        return path\n\n    def file('))
MUTATIONS.append(('v110: a folder out of the working folder entered', 'aihub/browse.py',
                 '    if not _inside(here, [root]) or not here.is_dir():\n', '    if not here.is_dir():\n'))
MUTATIONS.append(('v110: a link out of the working folder shown', 'aihub/browse.py',
                 '            if kind is None or not _inside(Path(entry.path).resolve(), [root]):\n', '            if kind is None:\n'))
MUTATIONS.append(('v110: a crowded folder listed whole', 'aihub/browse.py',
                 '        if len(entries) >= MAX_ENTRIES:\n', '        if False:\n'))
MUTATIONS.append(('v110: the parent of a subfolder lost', 'aihub/browse.py',
                 '"parent": "" if parent == "." else parent,', '"parent": parent,'))

MUTATIONS.append(('v110: going up out of the working folder', 'aihub/browse.py',
                 '    if raw and (raw.startswith(("/", "\\\\")) or ":" in raw or ".." in re.split(r"[\\\\/]", raw)):\n', '    if False:\n'))
MUTATIONS.append(('v110: a relative way out judged once changed', 'aihub/browse.py',
                 '    if raw and (raw.startswith(("/", "\\\\")) or ":" in raw or ".." in re.split(r"[\\\\/]", raw)):\n', '    if raw and (":" in raw or ".." in re.split(r"[\\\\/]", raw)):\n'))
MUTATIONS.append(('v110: a network path resolved', 'aihub/places.py',
                 '        if raw[:1] in ("\\\\", "/") or re.match(r"^[A-Za-z]:", raw):\n            local(raw)\n', ''))
# v110: "a network base resolved before the check" retired with its line (1.2.3): check_workdir is
# aihub/places.py, which refuses a network path before anything resolves it ("v110: a network path resolved").
MUTATIONS.append(('v110: a network vault looked at', 'aihub/vaults.py',
                 '    if os.name == "nt" and not re.match(r"^[A-Za-z]:[\\\\/]", path.strip()):\n', '    if False:\n'))

MUTATIONS.append(('v111: project vault ignored by its notes', 'aihub/obsidian.py',
                 '        if row is not None and row["obsidian_vault"]:\n            return row["obsidian_vault"]\n', ''))
MUTATIONS.append(('v111: a member chooses a project vault', 'aihub/web/app.py',
                 "            if body.obsidian_vault:\n                access.require_owner(user)  # a folder of the owner's PC\n", '            if body.obsidian_vault:\n'))
MUTATIONS.append(('v111: a member changes a project vault', 'aihub/web/app.py',
                 "            if body.obsidian_vault is not None:\n                access.require_owner(user)  # a folder of the owner's PC\n", '            if body.obsidian_vault is not None:\n'))
MUTATIONS.append(('v111: a project vault never checked', 'aihub/obsidian.py',
                 '        return str(Places(roots).folder(str(path), "coffre"))\n', '        return str(Path(str(path)))\n'))
# v111: "a relative project vault" retired: on Windows the local-disk check of
# check_project_vault refuses a relative path first; is_absolute() acts on Linux only.
MUTATIONS.append(('v111: plan note in the settings vault', 'aihub/web/jobs.py',
                 "            vault = obsidian.vault_of(conn, conversation, self.obsidian_vault)  # the project's, else the settings'\n", '            vault = self.obsidian_vault\n'))
MUTATIONS.append(('v111: rewrite in the settings vault', 'aihub/web/jobs.py',
                 "            vault = obsidian.vault_of(conn, conv, self.obsidian_vault)  # the project's, else the settings'\n", '            vault = self.obsidian_vault\n'))
MUTATIONS.append(('v111: reply noted in the settings vault', 'aihub/web/app.py',
                 '            path = obsidian.append(obsidian.vault_of(conn, conv, current().obsidian_vault), conv["obsidian_note"], conv["title"],', '            path = obsidian.append(current().obsidian_vault, conv["obsidian_note"], conv["title"],'))
MUTATIONS.append(('v111: an existing note taken without asking', 'aihub/web/app.py',
                 '                        elif body.obsidian_existing != "continue":\n', '                        elif False:\n'))
MUTATIONS.append(('v111: a new note overwrites the old one', 'aihub/web/app.py',
                 '                        if body.obsidian_existing == "new":\n                            note = obsidian.free_note_path(vault, note)\n', '                        if body.obsidian_existing == "new":\n                            pass\n'))
MUTATIONS.append(('v111: the free name is taken', 'aihub/obsidian.py',
                 '        if not resolve(vault, candidate).exists():\n', '        if True:\n'))
MUTATIONS.append(('v111: who writes there not said', 'aihub/web/app.py',
                 '                if obsidian.vault_of(conn, row, current().obsidian_vault) == vault]', '                if False]'))
MUTATIONS.append(('v111: project vaults unnamed in the window', 'aihub/vaults.py',
                 '        add(path, "projet")["projects"].append(name)\n', '        add(path, "projet")\n'))
MUTATIONS.append(("v111: a project's notes folder refused without .obsidian", 'aihub/web/app.py',
                 '        return vault if any(vaults._key(vault) == vaults._key(m) for m in mine) else None\n', '        return None\n'))

MUTATIONS.append(('v111: any error taken for a quota', 'aihub/store.py',
                 '    blocked = [st for st in plan["subtasks"] if st["status"] == "error" and QUOTA_ERROR.match(st["error"] or "")]\n', '    blocked = [st for st in plan["subtasks"] if st["status"] == "error"]\n'))
MUTATIONS.append(('v111: a running plan said stopped by a quota', 'aihub/store.py',
                 '    if plan["status"] not in ("error", "stopped"):\n        return None\n', ''))
MUTATIONS.append(('v111: a past quota return kept', 'aihub/store.py',
                 '        if moment is not None and moment > now:\n', '        if moment is not None:\n'))
MUTATIONS.append(('v111: tasks never started left on the blocked AI', 'aihub/store.py',
                 '    waiting = [st["position"] for st in plan["subtasks"] if st["status"] != "ok" and st["provider"] in providers]\n', '    waiting = [st["position"] for st in blocked]\n'))
MUTATIONS.append(('v111: a resume planned on an unresumable plan', 'aihub/store.py',
                 '    plan = _get_plan_row(conn, plan_id)\n    if not can_resume_plan(conn, plan):\n        raise StoreError("ce plan ne peut pas être repris : il doit avoir été validé et interrompu")\n    moment = _moment(at)\n', '    plan = _get_plan_row(conn, plan_id)\n    moment = _moment(at)\n'))
MUTATIONS.append(('v111: a resume planned in the past', 'aihub/store.py',
                 '    kept = max(moment, earliest).astimezone(timezone.utc).isoformat(timespec="seconds")\n', '    kept = moment.astimezone(timezone.utc).isoformat(timespec="seconds")\n'))
MUTATIONS.append(('v111: a resume kept in local time', 'aihub/store.py',
                 '    kept = max(moment, earliest).astimezone(timezone.utc).isoformat(timespec="seconds")\n', '    kept = max(moment, earliest).isoformat(timespec="seconds")\n'))
MUTATIONS.append(('v111: a resume planned weeks ahead', 'aihub/store.py',
                 '    if moment > earliest + timedelta(days=8):\n', '    if False:\n'))
MUTATIONS.append(('v111: a due resume run again', 'aihub/store.py',
                 '        conn.execute("UPDATE plans SET auto_resume_at = NULL WHERE id = ? AND auto_resume_at = ?",\n                     (row["id"], row["auto_resume_at"]))\n', '        pass\n'))
MUTATIONS.append(('v111: a manual resume leaves the planned one', 'aihub/store.py',
                 '        conn.execute("UPDATE plans SET status = \'validated\', finished_at = NULL, auto_resume_at = NULL WHERE id = ?",\n', '        conn.execute("UPDATE plans SET status = \'validated\', finished_at = NULL WHERE id = ?",\n'))
MUTATIONS.append(('v111: given to the AI whose quota it was', 'aihub/store.py',
                 '    if provider in plan["quota"]["providers"]:\n', '    if False:\n'))
MUTATIONS.append(('v111: given without a quota', 'aihub/store.py',
                 '    if not can_resume_plan(conn, plan) or plan["quota"] is None:\n', '    if not can_resume_plan(conn, plan):\n'))
MUTATIONS.append(('v111: the margin after the quota forgotten', 'aihub/web/app.py',
                 '                    at = (datetime.fromisoformat(back) + store.RESUME_MARGIN).isoformat()\n', '                    at = back\n'))
MUTATIONS.append(('v111: an unknown return guessed', 'aihub/web/app.py',
                 '                    if back is None:\n                        raise StoreError("l\'heure de retour du quota n\'est pas connue : choisissez une heure")\n', '                    if back is None:\n                        back = datetime.now(timezone.utc).isoformat()\n'))
MUTATIONS.append(("v111: a resume with someone else's accounts", 'aihub/web/app.py',
                 '        jobs.resume_execution(plan["conversation_id"], plan["id"], user=user)\n', '        jobs.resume_execution(plan["conversation_id"], plan["id"], user=None)\n'))
MUTATIONS.append(('v111: a planned resume skips the folder check', 'aihub/web/app.py',
                 '                raise StoreError("ce plan ne peut plus être repris")\n            check_workdir(conv["workdir"])\n', '                raise StoreError("ce plan ne peut plus être repris")\n'))
MUTATIONS.append(('v111: a refused resume not said', 'aihub/schedule.py',
                 '                        store.resume_failed(conn, plan["id"], str(exc))\n', '                        pass\n'))
# v111: the frozen plan opened to any change: retired with the v27 rule it aimed at (v28 replaces it; see the v112 ones).
# v111: the frozen plan opened to finished tasks: retired with the v27 rule it aimed at (v28 replaces it; see the v112 ones).

MUTATIONS.append(('v111: one word found is enough', 'aihub/vaults.py',
                 '        if not all(w in low or w in name for w in words):\n', '        if not any(w in low or w in name for w in words):\n'))
MUTATIONS.append(("v111: the note's name not searched", 'aihub/vaults.py',
                 '        if not all(w in low or w in name for w in words):\n', '        if not all(w in low for w in words):\n'))
MUTATIONS.append(('v111: search results not ranked', 'aihub/vaults.py',
                 '    found.sort(key=lambda f: (-f["hits"], f["path"].lower()))\n', ''))
MUTATIONS.append(('v111: search without end', 'aihub/vaults.py',
                 '        if looked >= MAX_SEARCH_NOTES or time.monotonic() > deadline:\n', '        if False:\n'))
MUTATIONS.append(('v111: an empty search', 'aihub/vaults.py',
                 '    if not words or len(query) > 200:\n', '    if len(query) > 200:\n'))
MUTATIONS.append(('v111: a search read whole notes', 'aihub/vaults.py',
                 '                text = fh.read(MAX_NOTE_BYTES).decode("utf-8", errors="replace")\n', '                text = fh.read().decode("utf-8", errors="replace")\n'))
MUTATIONS.append(('v111: the excerpt not around the word', 'aihub/vaults.py',
                 '                      "excerpt": _excerpt(text, min(places)) if places else _excerpt(text, 0)})\n', '                      "excerpt": _excerpt(text, 0)})\n'))
MUTATIONS.append(('v111: anyone searches a vault', 'aihub/web/app.py',
                 '    def vault_search(vault: str, q: str):\n        access.require_owner(me())\n', '    def vault_search(vault: str, q: str):\n'))
MUTATIONS.append(('v111: anyone reads a vault into a project', 'aihub/web/app.py',
                 "        access.require_owner(user)  # the vaults are folders of the owner's PC\n", ''))
# v111: "a note added to someone else's project" retired with its guard: the route is the owner's
# (require_owner first), and the owner manages every project, so require_project never refused.
MUTATIONS.append(('v111: a cut note added whole', 'aihub/web/app.py',
                 '        if note["cut"]:\n            raise StoreError("note trop longue pour la mémoire du projet (200 000 caractères au maximum)")\n', ''))
MUTATIONS.append(('v111: a linked note never read again', 'aihub/rag.py',
                 "        if row['source_mtime'] == stat.st_mtime and not row['source_missing']:\n            continue\n", '        continue\n'))
MUTATIONS.append(('v111: a gone note not said', 'aihub/rag.py',
                 "            if not row['source_missing']:\n                conn.execute('UPDATE rag_documents SET source_missing = 1 WHERE id = ?', (row['id'],))\n", '            pass\n'))
MUTATIONS.append(('v111: a changed note keeps its old passages', 'aihub/rag.py',
                 "            conn.execute('DELETE FROM rag_chunks WHERE document_id = ?', (row['id'],))\n", ''))
MUTATIONS.append(('v111: search before the note is read again', 'aihub/rag.py',
                 '    refresh_linked(conn, project_id)  # a note changed in Obsidian is searched as it is now\n', ''))
MUTATIONS.append(('v111: a link forgets where it comes from', 'aihub/rag.py',
                 "    conn.execute('UPDATE rag_documents SET source_path = ?, source_mtime = ?, source_missing = 0 WHERE id = ?',\n                 (str(source_path), mtime, did))\n", ''))

MUTATIONS.append(('v111: restored while the server still runs', 'aihub/restore.py',
                 '    if pid and alive(pid):\n        report = {"status": "echec"', '    if False:\n        report = {"status": "echec"'))
MUTATIONS.append(('v111: a server that does not come back keeps the backup', 'aihub/restore.py',
                 '            updater.copy_database(keep, db_path)\n', ''))
MUTATIONS.append(('v111: the restored server never checked', 'aihub/restore.py',
                 '        if report["status"] == "ok" and details and not (health or updater.wait_health)(process, details[1], __version__, probe):\n', '        if False:\n'))
MUTATIONS.append(('v111: two restores in one second', 'aihub/restore.py',
                 '    keep = db_path.with_name(f"avant-restauration-{datetime.now():%Y-%m-%d_%H%M%S}-{uuid.uuid4().hex[:6]}.db")\n', '    keep = db_path.with_name(f"avant-restauration-{datetime.now():%Y-%m-%d_%H%M%S}.db")\n'))
MUTATIONS.append(('v111: no report of the restore', 'aihub/restore.py',
                 '    _write_report(db_path, **report)\n    return report\n\n\ndef main', '    return report\n\n\ndef main'))
MUTATIONS.append(('v111: anyone restores', 'aihub/web/app.py',
                 '        user = me(request)\n        access.require_owner(user)\n        if authority is not None:\n            with database() as conn:\n                verified = authority.verify(conn, body.password, client_of(request.scope), name=user["name"])\n                if verified["id"] != user["id"]:\n                    raise access.Forbidden("les droits du compte ont changé : restauration refusée")', '        user = me(request)\n        if authority is not None:\n            with database() as conn:\n                verified = authority.verify(conn, body.password, client_of(request.scope), name=user["name"])\n                if verified["id"] != user["id"]:\n                    raise access.Forbidden("les droits du compte ont changé : restauration refusée")'))
MUTATIONS.append(('v111: a restore without the password', 'aihub/web/app.py',
                 '                verified = authority.verify(conn, body.password, client_of(request.scope), name=user["name"])\n                if verified["id"] != user["id"]:\n                    raise access.Forbidden("les droits du compte ont changé : restauration refusée")', '                verified = user\n                if verified["id"] != user["id"]:\n                    raise access.Forbidden("les droits du compte ont changé : restauration refusée")'))
MUTATIONS.append(('v111: anyone lists the backups', 'aihub/web/app.py',
                 '    def server_backups():\n        access.require_owner(me())\n', '    def server_backups():\n'))
MUTATIONS.append(('v111: restored while an AI works', 'aihub/web/app.py',
                 '            raise Busy("une IA travaille : attendez qu\'elle ait fini avant de restaurer")\n', '            pass\n'))
MUTATIONS.append(('v111: any file restored', 'aihub/web/app.py',
                 '        chosen = next((p for p in files if p.name == body.name), None)  # a name of the list, never a path\n', '        chosen = Path(folder) / body.name\n'))
MUTATIONS.append(('v111: an unreadable backup restored', 'aihub/web/app.py',
                 '                if check.execute("PRAGMA quick_check").fetchone()[0] != "ok":\n', '                if False:\n'))
MUTATIONS.append(('v111: a newer backup restored', 'aihub/web/app.py',
                 '                if check.execute("PRAGMA user_version").fetchone()[0] > db.SCHEMA_VERSION:\n', '                if False:\n'))

MUTATIONS.append(('v111: a zip bomb read', 'aihub/project_export.py',
                 '        if sum(i.file_size for i in infos.values()) > MAX_TOTAL or any(i.file_size > MAX_FILE for i in infos.values()):\n', '        if False:\n'))
MUTATIONS.append(('v111: a crowded zip read', 'aihub/project_export.py',
                 '        if len(infos) > MAX_FILES:\n', '        if False:\n'))
MUTATIONS.append(('v111: a huge zip read', 'aihub/project_export.py',
                 '    if len(data) > MAX_ZIP:\n', '    if False:\n'))
MUTATIONS.append(('v111: a conversation from anywhere in the zip', 'aihub/project_export.py',
                 '            if not isinstance(entry, dict) or not _CONVERSATION.match(str(entry.get("file"))):\n', '            if not isinstance(entry, dict):\n'))
MUTATIONS.append(('v111: a note from anywhere in the zip', 'aihub/project_export.py',
                 '            if note is not None and not _NOTE.match(str(note)):\n', '            if False:\n'))
MUTATIONS.append(('v111: a document from anywhere in the zip', 'aihub/project_export.py',
                 '            if not _DOCUMENT.match(str(path)) or ".." in str(path):\n', '            if False:\n'))
MUTATIONS.append(('v111: another format read', 'aihub/project_export.py',
                 '        if not isinstance(head, dict) or head.get("aihub_project") != FORMAT:\n', '        if not isinstance(head, dict):\n'))
# v111: "a project export shows hidden conversations" retired: who sees a project sees all its conversations (access rules of 1.1): the filter keeps the
# conversations list's own rule, for a future change of those rules; it cannot remove anything today.
MUTATIONS.append(('v111: a project export to who cannot see it', 'aihub/web/app.py',
                 '            if access.project_role(conn, user, pid) is None:\n                raise NotFound("projet introuvable")\n            project = dict(store.get_project(conn, pid))\n            policy', '            project = dict(store.get_project(conn, pid))\n            policy'))
MUTATIONS.append(('v111: an export reads the notes for anyone', 'aihub/web/app.py',
                 '                if conv["obsidian_note"] and (user is None or user.get("is_owner")):  # the vault is the owner\'s\n', '                if conv["obsidian_note"]:\n'))
MUTATIONS.append(('v111: an import over a project of the same name', 'aihub/web/app.py',
                 '                while name.lower() in existing:  # the same name is already a project here\n', '                while False:\n'))
# v111: "an import without its folder" retired: the early check was inert (new_conversation refuses, the transaction takes all back): removed.
MUTATIONS.append(('v111: an import half done', 'aihub/web/app.py',
                 '            with db.transaction(conn):\n                name, n = bundle["name"], 1\n', '            if True:\n                name, n = bundle["name"], 1\n'))
MUTATIONS.append(('v111: an imported note over an existing one', 'aihub/web/app.py',
                 '                    if obsidian.resolve(vault, note).exists():\n                        note = obsidian.free_note_path(vault, note)\n                    path = obsidian.resolve(vault, note)\n', '                    path = obsidian.resolve(vault, note)\n'))
MUTATIONS.append(('v111: an import writes notes for a member', 'aihub/web/app.py',
                 '                    if entry["note"] and copy_notes and (user is None or user.get("is_owner")):\n', '                    if entry["note"] and copy_notes:\n'))
MUTATIONS.append(('v111: an import by a guest', 'aihub/web/app.py',
                 '    async def import_project(request: HttpRequest, workdir: str = "", notes: bool = False):\n        """A project\'s .zip (made by « Exporter le projet »), as the request\'s body."""\n        user = me(request)\n        access.require_creator(user)\n', '    async def import_project(request: HttpRequest, workdir: str = "", notes: bool = False):\n        """A project\'s .zip (made by « Exporter le projet »), as the request\'s body."""\n        user = me(request)\n'))

MUTATIONS.append(('v111: anyone sees the trials', 'aihub/web/app.py',
                 '    def list_trials():\n        access.require_owner(me())\n', '    def list_trials():\n'))
MUTATIONS.append(('v111: anyone says how a trial went', 'aihub/web/app.py',
                 '    def say_trial(key: str, body: TrialSaid):\n        access.require_owner(me())\n', '    def say_trial(key: str, body: TrialSaid):\n'))
MUTATIONS.append(('v111: an unknown trial noted', 'aihub/trials.py',
                 '    if key not in KEYS:\n', '    if False:\n'))
MUTATIONS.append(('v111: any state for a trial', 'aihub/trials.py',
                 '    if status not in STATUSES:\n', '    if False:\n'))
MUTATIONS.append(('v111: an endless remark', 'aihub/trials.py',
                 '    if len(note) > MAX_NOTE:\n', '    if False:\n'))
MUTATIONS.append(('v111: a trial said once only', 'aihub/trials.py',
                 'ON CONFLICT(key) DO UPDATE SET status = excluded.status, note = excluded.note, ', 'ON CONFLICT(key) DO UPDATE SET status = status, note = note, '))

MUTATIONS.append(('v111: a restore that cannot come back', 'aihub/web/app.py',
                 '        if updater.restart_details(restart, updater.install_root()) is None:\n', '        if False:\n'))

MUTATIONS.append(('v112: a running task given away', 'aihub/db.py',
                 "                  AND OLD.status IN ('pending', 'error', 'stopped')\n", "                  AND OLD.status <> 'ok'\n"))
MUTATIONS.append(('v112: a finished task given away', 'aihub/db.py',
                 "                  AND OLD.status IN ('pending', 'error', 'stopped')\n", "                  AND OLD.status IN ('pending', 'error', 'stopped', 'ok')\n"))
MUTATIONS.append(('v112: a model changed alone', 'aihub/db.py',
                 "         AND NOT ((SELECT status FROM plans WHERE id = OLD.plan_id) IN ('validated', 'running', 'error', 'stopped')\n                  AND OLD.status IN ('pending', 'error', 'stopped')\n                  AND NEW.agent_id IS NOT NULL\n                  AND NEW.agent_id IS NOT OLD.agent_id   -- given to another agent, never a model changed alone\n", "         AND NOT ((SELECT status FROM plans WHERE id = OLD.plan_id) IN ('validated', 'running', 'error', 'stopped')\n                  AND OLD.status IN ('pending', 'error', 'stopped')\n                  AND NEW.agent_id IS NOT NULL\n"))
MUTATIONS.append(("v112: a task's content changed after validation", 'aihub/db.py',
                 '                  AND NEW.agent_id IS NOT OLD.agent_id   -- given to another agent, never a model changed alone\n                  AND NEW.description IS OLD.description\n                  AND NEW.position IS OLD.position\n                  AND NEW.title IS OLD.title\n                  AND NEW.files_json IS OLD.files_json\n                  AND NEW.kind IS OLD.kind)\n    BEGIN\n        SELECT RAISE(ABORT, \'plan is frozen\');\n    END\n    """,\n]\n\n# 1.2.2: a conversation\'s own Obsidian vault', '                  AND NEW.agent_id IS NOT OLD.agent_id   -- given to another agent, never a model changed alone\n                  AND NEW.position IS OLD.position\n                  AND NEW.title IS OLD.title\n                  AND NEW.files_json IS OLD.files_json\n                  AND NEW.kind IS OLD.kind)\n    BEGIN\n        SELECT RAISE(ABORT, \'plan is frozen\');\n    END\n    """,\n]\n\n# 1.2.2: a conversation\'s own Obsidian vault'))
MUTATIONS.append(('v112: a proposed or finished plan switched', 'aihub/store.py',
                 '    if plan["status"] not in SWITCHABLE and not can_resume_plan(conn, plan):\n', '    if False:\n'))
MUTATIONS.append(('v112: running tasks switched too', 'aihub/store.py',
                 '    moved = [st for st in plan["subtasks"] if st["status"] in ("pending", "error", "stopped") and st["agent_id"] != agent_id]\n', '    moved = [st for st in plan["subtasks"] if st["status"] != "ok" and st["agent_id"] != agent_id]\n'))
MUTATIONS.append(('v112: the conductor not changed', 'aihub/web/app.py',
                 '            if body.conductor:\n                store.set_conductor(conn, conv["id"], agent)\n', ''))
MUTATIONS.append(('v112: the conductor changed when asked not to', 'aihub/web/app.py',
                 '            if body.conductor:\n                store.set_conductor(conn, conv["id"], agent)\n', '            if True:\n                store.set_conductor(conn, conv["id"], agent)\n'))
MUTATIONS.append(('v112: the synthesis by the former conductor', 'aihub/orchestrator.py',
                 '        conversation = store.get_conversation(conn, conversation["id"])\n        conductor = _conductor(conn, conversation)\n', '        conductor = _conductor(conn, conversation)\n'))
MUTATIONS.append(('v112: an interrupted plan not resumed', 'aihub/web/app.py',
                 '        if resume_now:\n            jobs.resume_execution(plan["conversation_id"], pid, user=user)\n', '        if False:\n            pass\n'))
MUTATIONS.append(('v112: a reader switches the plan', 'aihub/web/app.py',
                 '            conv = access.require(conn, user, plan["conversation_id"], "write")\n            agent = agent_for(conn, body.model, isolated=isolation.effective(conn, conv)["isolated"])\n            moved = store.switch_plan(', '            conv = access.require(conn, user, plan["conversation_id"], "read")\n            agent = agent_for(conn, body.model, isolated=isolation.effective(conn, conv)["isolated"])\n            moved = store.switch_plan('))

# v113 (1.2): the code on disk seen, tried, restarted on; a conversation without a folder gets its own.
MUTATIONS.append(('v113: a restart without a new commit', 'aihub/relaunch.py',
                 '    moved = is_git and started.get("commit") is not None and commit != started["commit"]\n',
                 '    moved = is_git and started.get("commit") is not None\n'))
MUTATIONS.append(('v113: the version change not seen', 'aihub/relaunch.py',
                 '"restart_needed": bool(moved or (disk is not None and disk != started["version"]))}', '"restart_needed": bool(moved)}'))
MUTATIONS.append(('v113: work in progress counted as none', 'aihub/relaunch.py',
                 '        dirty = None if status is None else sorted({line[3:].strip() for line in status.splitlines() if line.strip()})\n',
                 '        dirty = []\n'))
MUTATIONS.append(('v113: files outside the code count', 'aihub/relaunch.py',
                 '        status = git(root, "status", "--porcelain", "--untracked-files=no", "--", *CODE_PATHS)\n',
                 '        status = git(root, "status", "--porcelain", "--untracked-files=no")\n'))
MUTATIONS.append(('v113: the trial migrates the base itself', 'aihub/relaunch.py',
                 '            updater.copy_database(db_path, copy)\n            conn = db.connect(str(copy))\n',
                 '            conn = db.connect(str(db_path))\n'))
MUTATIONS.append(('v113: a dead trial taken as passed', 'aihub/relaunch.py',
                 '            return {"ok": False, "error": "le nouveau code s\'est arrêté avant de répondre (erreur au chargement)"}\n',
                 '            return {"ok": True}\n'))
MUTATIONS.append(('v113: a failed restart keeps the migrated base', 'aihub/relaunch.py',
                 '                updater.copy_database(keep, db_path)  # the base from before the schema change\n', '                pass\n'))
MUTATIONS.append(('v113: no second start after a failure', 'aihub/relaunch.py',
                 '            launch()\n            report = {"status": "echec", "version": version, "keep": str(keep) if keep else None,\n',
                 '            report = {"status": "echec", "version": version, "keep": str(keep) if keep else None,\n'))
MUTATIONS.append(('v113: the new server not checked', 'aihub/relaunch.py',
                 '        if details and not (health or updater.wait_health)(process, details[1], version, probe):\n', '        if False:\n'))
MUTATIONS.append(('v113: a server that did not stop started again', 'aihub/relaunch.py',
                 '    if pid and alive(pid):\n        report = {"status": "echec", "version": version,\n',
                 '    if False:\n        report = {"status": "echec", "version": version,\n'))
MUTATIONS.append(('v113: a guest restarts the server', 'aihub/web/app.py',
                 '        access.require_owner(me())\n        if jobs.busy():\n            raise Busy("une IA travaille : attendez qu\'elle ait fini avant de redémarrer")\n',
                 '        if jobs.busy():\n            raise Busy("une IA travaille : attendez qu\'elle ait fini avant de redémarrer")\n'))
MUTATIONS.append(('v113: a restart while an AI works', 'aihub/web/app.py',
                 '        if jobs.busy():\n            raise Busy("une IA travaille : attendez qu\'elle ait fini avant de redémarrer")\n', ''))
MUTATIONS.append(('v113: a restart with nothing new', 'aihub/web/app.py',
                 '        if not state["restart_needed"]:\n', '        if False:\n'))
MUTATIONS.append(('v113: a restart over work in progress', 'aihub/web/app.py',
                 '        if state["dirty"]:\n', '        if False:\n'))
MUTATIONS.append(('v113: a server that cannot come back stopped', 'aihub/web/app.py',
                 '        if updater.restart_details(restart, root) is None:\n', '        if False:\n'))
MUTATIONS.append(('v113: a broken new code restarted', 'aihub/web/app.py',
                 '        if not trial.get("ok"):\n', '        if False:\n'))
MUTATIONS.append(('v113: no copy before a schema change', 'aihub/web/app.py',
                 '        keep = relaunch.keep_copy(db_path, version) if trial.get("schema", schema) != schema else None\n',
                 '        keep = None\n'))
MUTATIONS.append(('v113: a copy at every restart', 'aihub/web/app.py',
                 '        keep = relaunch.keep_copy(db_path, version) if trial.get("schema", schema) != schema else None\n',
                 '        keep = relaunch.keep_copy(db_path, version)\n'))
MUTATIONS.append(('v113: no folder, refused as before', 'aihub/web/app.py',
                 "                if (profile['isolated'] and profile['files'] == 'none') or auto_folder:\n",
                 "                if profile['isolated'] and profile['files'] == 'none':\n"))
MUTATIONS.append(('v113: an import gets a folder of its own', 'aihub/web/app.py',
                 '    def new_conversation(conn, body, user=None, auto_folder=False):\n',
                 '    def new_conversation(conn, body, user=None, auto_folder=True):\n'))
MUTATIONS.append(("v113: the app's own folder checked against the allowed ones", 'aihub/web/app.py',
                 '            if not made:\n                workdir = check_workdir(workdir)\n', '            workdir = check_workdir(workdir)\n'))
MUTATIONS.append(('v114: the conversation\'s own vault ignored', 'aihub/obsidian.py',
                 '    if conversation["obsidian_vault"]:\n        return conversation["obsidian_vault"]\n', ''))
MUTATIONS.append(('v114: anyone chooses a folder of the PC as a vault', 'aihub/web/app.py',
                 '                access.require_owner(user)\n                vault = obsidian.check_project_vault(body.obsidian_vault, current().allowed_roots)\n',
                 '                vault = obsidian.check_project_vault(body.obsidian_vault, current().allowed_roots)\n'))
MUTATIONS.append(('v114: any path taken as a vault', 'aihub/web/app.py',
                 '                access.require_owner(user)\n                vault = obsidian.check_project_vault(body.obsidian_vault, current().allowed_roots)\n',
                 '                access.require_owner(user)\n                vault = body.obsidian_vault or None\n'))
MUTATIONS.append(('v114: the note of the former vault kept', 'aihub/web/app.py',
                 '                    store.set_conversation_vault(conn, cid, vault)\n                    # The note of the former vault stays there; one is made in the new vault when linked.\n                    store.set_obsidian_note(conn, cid, None)\n',
                 '                    store.set_conversation_vault(conn, cid, vault)\n'))
MUTATIONS.append(('v114: the same vault again unlinks', 'aihub/web/app.py',
                 '                if vault != store.get_conversation(conn, cid)["obsidian_vault"]:\n', '                if True:\n'))
MUTATIONS.append(('v114: the vault not saved', 'aihub/store.py',
                 '    conn.execute("UPDATE conversations SET obsidian_vault = ? WHERE id = ?", (vault, conversation_id))\n', ''))
MUTATIONS.append(("v114: the vault reader blind to conversations' vaults", 'aihub/web/app.py',
                 '                "SELECT obsidian_vault FROM projects WHERE obsidian_vault IS NOT NULL "\n                "UNION SELECT obsidian_vault FROM conversations WHERE obsidian_vault IS NOT NULL")]\n',
                 '                "SELECT obsidian_vault FROM projects WHERE obsidian_vault IS NOT NULL")]\n'))
MUTATIONS.append(('v113: two conversations in one folder', 'aihub/web/app.py',
                 '        while folder.exists() and (fresh or any(folder.iterdir())):\n',
                 '        while folder.exists() and any(folder.iterdir()):\n'))

# v115 (1.2.3): one rule for the PC's files and folders (aihub/places.py), and what the explorer does with them.
MUTATIONS.append(('v115: a path name taken whole', 'aihub/places.py',
                 ' or _BAD_NAME.search(name.strip())\n', '\n'))
MUTATIONS.append(('v115: any kind of file read', 'aihub/places.py',
                 '        if extensions is not None and path.suffix.lower() not in extensions:\n', '        if False:\n'))
MUTATIONS.append(('v115: a file of any size read', 'aihub/places.py',
                 '        if max_size is not None and path.stat().st_size > max_size:\n', '        if False:\n'))
MUTATIONS.append(('v115: a folder read as a file', 'aihub/places.py',
                 '        if not path.is_file():\n            raise StoreError(f"ce {what} n\'existe pas : {path}")\n', ''))
MUTATIONS.append(('v115: a free name that is taken', 'aihub/places.py',
                 '    if not path.exists():\n        return path\n', ''))
MUTATIONS.append(('v115: a member reads the PC', 'aihub/web/app.py',
                 '        if not access.trusted(user):\n            raise access.Forbidden("les fichiers du PC serveur sont réservés au propriétaire et aux administrateurs")\n', ''))
MUTATIONS.append(('v115: a file read outside the allowed folders', 'aihub/web/app.py',
                 '        return allowed_places().file(raw, **limits)\n', '        return places.Places().file(raw, **limits)\n'))
MUTATIONS.append(('v115: any file into the memory', 'aihub/web/app.py',
                 '        path = pc_file(user, body.path, extensions=MEMORY_KINDS, max_size=MEMORY_FILE)\n', '        path = pc_file(user, body.path)\n'))
MUTATIONS.append(('v115: a file not in UTF-8 into the memory', 'aihub/web/app.py',
                 '            content = path.read_text(encoding="utf-8")\n        except UnicodeDecodeError:\n',
                 '            content = path.read_text(encoding="utf-8", errors="replace")\n        except UnicodeDecodeError:\n'))
MUTATIONS.append(('v115: a member writes on the PC', 'aihub/web/app.py',
                 '        if not access.trusted(user):\n            raise access.Forbidden("enregistrer sur le PC serveur est réservé au propriétaire et aux administrateurs")\n', ''))
MUTATIONS.append(('v115: a saved file without the password', 'aihub/web/app.py',
                 '        with database() as conn:\n            confirm_maker(conn, user, body.password, request)\n        target = places.free(',
                 '        target = places.free('))
MUTATIONS.append(('v115: a saved file over another one', 'aihub/web/app.py',
                 '        target = places.free(allowed_places().child(body.folder, name))\n', '        target = allowed_places().child(body.folder, name)\n'))
MUTATIONS.append(('v115: any format saved', 'aihub/web/app.py',
                 '        if body.format not in ("md", "json"):\n            raise StoreError("format : md ou json")\n', ''))
MUTATIONS.append(('v115: a vault outside the allowed folders offered', 'aihub/web/app.py',
                 '"allowed": bool(v["exists"]) and rule.allows(Path(v["path"]))', '"allowed": bool(v["exists"])'))
MUTATIONS.append(('v115: a vault outside the allowed folders read', 'aihub/web/app.py',
                 '            allowed_places().folder(str(vault or ""), "coffre")\n', '            pass\n'))
MUTATIONS.append(('v115: a vault chosen outside the allowed folders', 'aihub/obsidian.py',
                 '        return str(Places(roots).folder(str(path), "coffre"))\n', '        return str(Places().folder(str(path), "coffre"))\n'))
MUTATIONS.append(('v115: a network folder in the settings resolved', 'aihub/config.py',
                 '    if os.name == "nt" and not re.match(r"^[A-Za-z]:[\\\\/]", value.strip()):\n        # Never looked at',
                 '    if False:\n        # Never looked at'))
MUTATIONS.append(('v115: a huge folder read whole', 'aihub/browse.py',
                 '                if len(items) >= MAX_SCAN:\n', '                if False:\n'))

# v116 (1.2.4): the update checked against the release's SHA256SUMS, undone without the new code
# (old dependencies back), its failure explained in the journal; the server stops without waiting 20 s.
MUTATIONS.append(('v116: a stopped server held by its last resort', 'aihub/web/app.py',
                 '        last_resort.daemon = True\n', ''))
MUTATIONS.append(("v116: the update probe left to the AIs' processes", 'aihub/web/app.py',
                 "    update_probe = os.environ.pop('AIHUB_UPDATE_PROBE', None)\n",
                 "    update_probe = os.environ.get('AIHUB_UPDATE_PROBE')\n"))
MUTATIONS.append(('v116: the health asks the environment again', 'aihub/web/app.py',
                 "        return {'ready': True, 'version': __version__, 'probe': update_probe}\n",
                 "        return {'ready': True, 'version': __version__, 'probe': os.environ.get('AIHUB_UPDATE_PROBE')}\n"))
MUTATIONS.append(('v116: the undoing runs the new code', 'aihub/updater.py',
                 '        if process is not None:\n            _kill_tree(process)\n',
                 '        if process is not None:\n            from .providers.claude_code import _kill_tree as late\n'
                 '            late(process)\n'))
MUTATIONS.append(('v116: SHA256SUMS of another release', 'aihub/updater.py',
                 '"page": release.get("html_url"), "sums": sums})', '"page": release.get("html_url"), "sums": None})'))
MUTATIONS.append(('v116: a release without SHA256SUMS not said', 'aihub/updater.py',
                 '    if not sums:\n        raise UpdateError(', '    if False:\n        raise UpdateError('))
MUTATIONS.append(('v116: SHA256SUMS from anywhere', 'aihub/updater.py',
                 '    if not sums.startswith(f"https://github.com/{REPO}/releases/download/"):\n', '    if False:\n'))
MUTATIONS.append(('v116: the SHA256 not compared', 'aihub/updater.py',
                 '        if hashlib.sha256(data).hexdigest() != published_sha256(sums, target.name, get):\n',
                 '        if published_sha256(sums, target.name, get) is None:\n'))
MUTATIONS.append(('v116: SHA256SUMS of any size', 'aihub/updater.py',
                 '    if len(data) > MAX_SUMS:\n', '    if False:\n'))
MUTATIONS.append(('v116: SHA256SUMS out of reach not refused', 'aihub/updater.py',
                 '        raise UpdateError(f"liste d\'empreintes SHA256SUMS impossible à lire : {exc}"[:300]) from None\n',
                 '        raise\n'))
MUTATIONS.append(('v116: a malformed SHA256SUMS line read', 'aihub/updater.py',
                 '    if not entries or None in entries:\n', '    if not entries:\n'))
MUTATIONS.append(('v116: an empty SHA256SUMS read', 'aihub/updater.py',
                 '    if not entries or None in entries:\n', '    if None in entries:\n'))
MUTATIONS.append(('v116: no SHA256SUMS line for the file', 'aihub/updater.py',
                 '    if len(found) != 1:\n', '    if len(found) > 1:\n'))
MUTATIONS.append(('v116: two SHA256SUMS lines for the file', 'aihub/updater.py',
                 '    if len(found) != 1:\n', '    if not found:\n'))
MUTATIONS.append(('v116: the saved base keeps its write-ahead log', 'aihub/updater.py',
                 '            copy_database(database, saved_db, standalone=True)\n', '            copy_database(database, saved_db)\n'))
MUTATIONS.append(('v116: a standalone copy in write-ahead mode', 'aihub/updater.py',
                 "                target.execute('PRAGMA journal_mode = DELETE')\n", '                pass\n'))
MUTATIONS.append(('v116: a failed pip not undone', 'aihub/updater.py',
                 '            pip_ran = True  # even a pip that fails may have changed the venv\n            install_requirements()\n',
                 '            install_requirements()\n            pip_ran = True\n'))
MUTATIONS.append(('v116: the old dependencies not put back', 'aihub/updater.py',
                 '        if pip_ran and requirements_changed:\n', '        if False:\n'))
MUTATIONS.append(('v116: the same dependencies installed again', 'aihub/updater.py',
                 '        if pip_ran and requirements_changed:\n', '        if pip_ran:\n'))
MUTATIONS.append(('v116: requirements compared with their line ends', 'aihub/updater.py',
                 '    return path.read_bytes().splitlines() if path.is_file() else None\n',
                 '    return path.read_bytes() if path.is_file() else None\n'))
MUTATIONS.append(('v116: a failed reinstall stops the undoing', 'aihub/updater.py',
                 '            except Exception as exc:  # the old server is started all the same\n',
                 '            except OSError as exc:  # the old server is started all the same\n'))
MUTATIONS.append(('v116: the exit code of the new server not said', 'aihub/updater.py',
                 '            cause = f"le nouveau serveur s\'est arrêté (code de sortie {code})"\n', '            cause = None\n'))
MUTATIONS.append(('v116: the last answer of the new server not said', 'aihub/updater.py',
                 '        why.append(cause or f"délai de {timeout} s dépassé ; dernier essai : {seen}")\n',
                 '        why.append(cause or f"délai de {timeout} s dépassé")\n'))
MUTATIONS.append(('v116: the cause not in the journal', 'aihub/updater.py',
                 "                raise UpdateError(f'la nouvelle version ne répond pas correctement ({cause}) ; ancienne version rétablie')\n",
                 "                raise UpdateError('la nouvelle version ne répond pas correctement ; ancienne version rétablie')\n"))
MUTATIONS.append(('v116: the server log not shown', 'aihub/updater.py',
                 '                if end:\n', '                if False:\n'))
MUTATIONS.append(('v116: the server log read from before the restart', 'aihub/updater.py',
                 '            written = log.stat().st_size if log is not None and log.is_file() else 0\n', '            written = 0\n'))
MUTATIONS.append(('v116: the whole server log shown', 'aihub/updater.py',
                 "    return [line.rstrip()[:300] for line in text.splitlines() if line.strip()][-lines:]\n",
                 "    return [line.rstrip()[:300] for line in text.splitlines() if line.strip()]\n"))
MUTATIONS.append(('v116: blank lines of the server log shown', 'aihub/updater.py',
                 "    return [line.rstrip()[:300] for line in text.splitlines() if line.strip()][-lines:]\n",
                 "    return [line.rstrip()[:300] for line in text.splitlines()][-lines:]\n"))
MUTATIONS.append(('v116: a server log begun again not read', 'aihub/updater.py',
                 '            fh.seek(max(start if start <= size else 0, size - 64 * 1024))',
                 '            fh.seek(max(start, size - 64 * 1024))'))

# v117 (1.3.0): Claude Code, Codex and sbx searched for at each use (aihub/locate.py), never kept from the
# launch; « Chercher sur ce PC » in Réglages; the path kept in config.json; nothing of it in an isolated space.
MUTATIONS.append(('v117: a vanished copy kept by the cache', 'aihub/locate.py',
                 'now - kept[0] < CACHE_S and Path(kept[1].path).is_file():', 'now - kept[0] < CACHE_S:'))
MUTATIONS.append(('v117: the cache kept forever', 'aihub/locate.py',
                 'not refresh and now - kept[0] < CACHE_S and', 'not refresh and'))
MUTATIONS.append(('v117: a vanished file started without a new search', 'aihub/locate.py',
                 '    searched = not Path(command[0]).is_file()\n', '    searched = False\n'))
MUTATIONS.append(('v117: no new try after FileNotFoundError', 'aihub/locate.py',
                 '        if searched:\n            raise _no_start(command) from None\n',
                 '        raise _no_start(command) from None\n'))
MUTATIONS.append(('v117: a second new try after FileNotFoundError', 'aihub/locate.py',
                 '        if searched:\n            raise _no_start(command) from None\n', '        pass\n'))
MUTATIONS.append(('v117: a .cmd of the PATH before a native .exe', 'aihub/locate.py',
                 '    for names in ([f"{program}.exe"], [f"{program}.cmd", f"{program}.bat", f"{program}.com"]):\n',
                 '    for names in ([f"{program}.cmd", f"{program}.bat", f"{program}.com", f"{program}.exe"],):\n'))
MUTATIONS.append(("v117: npm's shim instead of its native copy", 'aihub/locate.py',
                 '        native = _native_of(program, hit, system)\n', '        native = None\n'))
MUTATIONS.append(('v117: a newer .cmd before a native copy', 'aihub/locate.py',
                 '    return max(copies, key=lambda c: (not _script(c.path), c.mtime, -c.index))\n',
                 '    return max(copies, key=lambda c: (c.mtime, -c.index))\n'))
MUTATIONS.append(('v117: the first place before the newest copy', 'aihub/locate.py',
                 '    return max(copies, key=lambda c: (not _script(c.path), c.mtime, -c.index))\n',
                 '    return max(copies, key=lambda c: (not _script(c.path), -c.index))\n'))
MUTATIONS.append(('v117: the oldest copy of a place', 'aihub/locate.py',
                 '(_version_key(copy.version), copy.mtime) > (_version_key(kept.version), kept.mtime)',
                 '(_version_key(copy.version), copy.mtime) < (_version_key(kept.version), kept.mtime)'))
MUTATIONS.append(('v117: a copy of WindowsApps taken', 'aihub/locate.py',
                 '    if any(part.lower() == "windowsapps" for part in path.parts):\n', '    if False:\n'))
MUTATIONS.append(('v117: the PATH stops at WindowsApps', 'aihub/locate.py',
                 '                if os.path.isfile(hit) and refused(program, hit) is None:\n',
                 '                if os.path.isfile(hit):\n'))
MUTATIONS.append(('v117: the current folder taken for the PATH', 'aihub/locate.py',
                 ' if d and os.path.isabs(d)]', ' if d]'))
MUTATIONS.append(('v117: a desktop app taken for its CLI', 'aihub/locate.py',
                 '    if ((folder / "Update.exe").exists() or (folder / "icudtl.dat").exists()\n'
                 '            or (folder / "resources" / "app.asar").exists()):\n', '    if False:\n'))
MUTATIONS.append(('v117: a path kept that has gone still used', 'aihub/locate.py',
                 '        if Path(given).is_file():\n            return Found(', '        if True:\n            return Found('))
MUTATIONS.append(('v117: a path kept that has gone not said', 'aihub/locate.py',
                 '        notes.append(_gone(program, origin, given))\n', '        pass\n'))
MUTATIONS.append(('v117: the native installer of Linux not looked at', 'aihub/locate.py',
                 '    common = [("native", home / ".local" / "bin", program, False),\n',
                 '    common = [("native", home / ".local" / "lib", program, False),\n'))
MUTATIONS.append(('v117: the scan without a depth', 'aihub/locate.py',
                 'if depth > 0 and name.lower() not in _SKIP', 'if name.lower() not in _SKIP'))
MUTATIONS.append(('v117: the scan without a time limit', 'aihub/locate.py',
                 '        if looked >= max_dirs or clock() > deadline:\n', '        if looked >= max_dirs:\n'))
MUTATIONS.append(('v117: the scan without a limit of folders', 'aihub/locate.py',
                 '        if looked >= max_dirs or clock() > deadline:\n', '        if clock() > deadline:\n'))
MUTATIONS.append(('v117: an unreadable folder stops the scan', 'aihub/locate.py',
                 '        except OSError:\n            continue  # unreadable (access denied, gone): passed over\n',
                 '        except FileNotFoundError:\n            continue  # unreadable (access denied, gone): passed over\n'))
MUTATIONS.append(('v117: the scan follows links', 'aihub/locate.py',
                 '                                    and not _linked(entry) \\\n', '                                    and True \\\n'))
MUTATIONS.append(('v117: an older .sandbox-bin shown', 'aihub/locate.py',
                 '                and any(_mtime(other.path) > _mtime(found.path) for other in codexes if other is not found):\n',
                 '                and False:\n'))
MUTATIONS.append(('v117: a file met by the scan run', 'aihub/locate.py',
                 'if trusted and found.version is None', 'if found.version is None'))
MUTATIONS.append(('v117: a network folder scanned', 'aihub/locate.py',
                 'if folder is not None and _LOCAL.match(str(folder))]', 'if folder is not None]'))
MUTATIONS.append(('v117: an isolated space searches the PC', 'aihub/providers/__init__.py',
                 "n'est pas branché sur ce serveur\")\n    if execution is not None:\n",
                 "n'est pas branché sur ce serveur\")\n    if isinstance(given, dict):\n"
                 "        return module(provider).find_cli()\n    if execution is not None:\n"))
MUTATIONS.append(("v117: an isolated space's Codex taken from the PC", 'aihub/providers/codex.py',
                 "                command, spawn = ['codex'], execution.spawn\n", '                spawn = execution.spawn\n'))
MUTATIONS.append(('v117: a delegation forgets the path kept', 'aihub/providers/__init__.py',
                 '        return module(provider).find_cli(option=given.get("option"), setting=given.get("setting"))\n',
                 '        return module(provider).find_cli()\n'))
MUTATIONS.append(('v117: the path kept not tried before a restart', 'aihub/web/app.py',
                 '                commands[provider]["setting"] = kept\n', '                pass\n'))
MUTATIONS.append(('v117: the state read before the path kept', 'aihub/web/app.py',
                 '            status_cache.pop("all", None)  # the state of the AIs is read again with it\n',
                 '            pass\n'))
MUTATIONS.append(('v117: a member scans the PC', 'aihub/web/app.py',
                 '        access.require_owner(me())\n        if not scan_lock.acquire(blocking=False):\n',
                 '        if not scan_lock.acquire(blocking=False):\n'))
MUTATIONS.append(('v117: the diagnostic gives the path', 'aihub/web/app.py',
                 "'detail': f'{name} trouvé ({where}).' if where",
                 "'detail': f'{name} trouvé ({where}) : {providers.command_for(commands, provider)[0]}.' if where"))
MUTATIONS.append(("v117: the pill shows an exception's text", 'aihub/web/app.py',
                 '    if state["error"]:\n        text = (f"{name} est trouvé',
                 '    if state["error"]:\n        return state["error"]\n        text = (f"{name} est trouvé'))
MUTATIONS.append(('v117: the launch paths fixed or lost', 'aihub/cli.py',
                 '                     cli_paths={claude_code.PROVIDER: args.claude, codex.PROVIDER: args.codex},\n',
                 '                     cli_paths=None,\n'))
MUTATIONS.append(('v117: a program kept that is not there', 'aihub/config.py',
                 '    if not Path(value).is_file():\n        raise ConfigError(f"{what} : fichier introuvable ({value})")\n', ''))
MUTATIONS.append(('v117: any file kept as a program', 'aihub/config.py',
                 '    reason = refused(program, value)\n', '    reason = None\n'))
MUTATIONS.append(('v117: a program gone stops the server', 'aihub/config.py',
                 '        return validate(data, check_programs=False)\n', '        return validate(data)\n'))
MUTATIONS.append(('v117: a network path of a program resolved', 'aihub/config.py',
                 '    if os.name == "nt" and not re.match(r"^[A-Za-z]:[\\\\/]", value):\n', '    if False:\n'))
# Review of 1.3.0: a fake Claude alone offers no real Codex; a .cmd never gets what cmd.exe cuts or runs.
MUTATIONS.append(('v117: a Claude command alone offers the real Codex', 'aihub/providers/__init__.py',
                 'commands if provider == claude_code.PROVIDER else False for provider in MODULES}',
                 'commands if provider == claude_code.PROVIDER else None for provider in MODULES}'))
MUTATIONS.append(('v117: a delegation under a Claude command alone searches Codex', 'aihub/tools.py',
                 'json.dumps(providers.by_provider(commands))',
                 'json.dumps(commands if isinstance(commands, dict) else {"claude_code": commands})'))
MUTATIONS.append(('v117: ask --claude offers Claude Code only', 'aihub/cli.py',
                 '{claude_code.PROVIDER: claude_code.find_cli(args.claude)} if args.claude else None))',
                 'claude_code.find_cli(args.claude) if args.claude else None))'))
MUTATIONS.append(('v117: a .cmd started with what cmd.exe cuts or runs', 'aihub/locate.py',
                 '    if os.name == "nt" and _script(command[0]) and _cmd_changes([*command, *tail]):\n',
                 '    if False:\n'))
MUTATIONS.append(('v117: a line break passed to cmd.exe', 'aihub/locate.py',
                 'if "\\n" in line or "\\r" in line or re.search', 'if re.search'))
MUTATIONS.append(('v117: a %NAME% passed to cmd.exe', 'aihub/locate.py',
                 ' or re.search(r"%[^%\\s]+%", line):', ':'))
MUTATIONS.append(('v117: a & between quotes refused to a .cmd', 'aihub/locate.py',
                 '        elif char in "&|<>^" and not quoted:\n', '        elif char in "&|<>^":\n'))
MUTATIONS.append(('v117: the copies to keep not named for a screen reader', 'aihub/web/static/app.js',
                 ', "aria-label": `Retenir ${c.path}`', ''))

TEST_MODULES = []


def run_tests(root, timeout=300):
    proc = subprocess.Popen(
        [sys.executable, "-B", "-m", "unittest"] + (TEST_MODULES or
         ["discover", "-s", str(root / "tests"), "-t", str(root)]),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW, cwd=root,
    )
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # A hung suite counts as a failure; its whole process tree goes.
        _kill_tree(proc)
        return 1, ["(suite bloquée, arrêtée après %d s)" % timeout], "TIMEOUT"
    fallen = sorted(set(re.findall(r"^(?:FAIL|ERROR): (\w+)", out, re.M)))
    summary = out.strip().splitlines()[-1] if out.strip() else "?"
    return proc.returncode, fallen, summary


def _pause_process():
    """The program, among AIHUB_BENCH_PAUSE_IF (name prefixes, comma separated), that is running, or None.

    A heavy program (a game, say) the machine's owner wants left alone: the
    bench stops while it runs."""
    prefixes = [p.strip().lower() for p in os.environ.get("AIHUB_BENCH_PAUSE_IF", "").split(",") if p.strip()]
    if not prefixes or os.name != "nt":
        return None
    out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True,
                         errors="replace", creationflags=NO_WINDOW).stdout
    for line in out.splitlines():
        name = line.split(",")[0].strip('"')
        if any(name.lower().startswith(p) for p in prefixes):
            return name
    return None


def _try(index, mutation, base, tmp):
    """Apply one mutation in its own copy and run the suite. Returns a report line and survival."""
    name, rel, old, new = mutation
    busy = _pause_process()
    if busy:
        return f"[{name}] NON LANCEE : {busy} tourne, le banc s'arrête", True
    work = Path(tmp) / f"mut-{index}"
    shutil.copytree(base, work)
    try:
        target = work / rel
        text = target.read_text(encoding="utf-8")
        n = text.count(old)
        if n != 1:
            return f"[{name}] FRAGMENT TROUVE {n} FOIS : mutation non appliquee", True
        target.write_text(text.replace(old, new), encoding="utf-8")
        assert target.read_text(encoding="utf-8").count(new) >= 1
        code, fallen, summary = run_tests(work, TIMEOUT)
        if summary == "TIMEOUT":
            # Too slow is not caught: a suite that only ran out of time proves nothing.
            return f"[{name}] INDECISE : suite arrêtée après {TIMEOUT} s", True
        if code == 0:
            return f"[{name}] SURVIT", True
        return f"[{name}] tuee par {len(fallen)} test(s) : {', '.join(fallen)}", False
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    import argparse
    from concurrent.futures import ThreadPoolExecutor

    parser = argparse.ArgumentParser()
    parser.add_argument("--jobs", type=int, default=1,
                        help="mutations traitées en même temps, chacune dans sa copie (défaut : 1)")
    parser.add_argument("--only", action="append", default=[], metavar="MOT",
                        help="ne lancer que les mutations dont le nom contient ce mot (répétable)")
    parser.add_argument("--timeout", type=int, default=900,
                        help="secondes avant d'arrêter une suite bloquée (défaut : 900)")
    parser.add_argument("--tests", action="append", default=[], metavar="MODULE",
                        help="modules de tests ciblés (répétable) ; sans cette option, toute la suite")
    args = parser.parse_args()
    global TIMEOUT, TEST_MODULES
    TIMEOUT = args.timeout
    TEST_MODULES = args.tests
    mutations = [m for m in MUTATIONS if not args.only or any(w in m[0] for w in args.only)]
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp) / "orig"
        shutil.copytree(SRC, base, ignore=shutil.ignore_patterns("__pycache__", ".git", "tools", ".venv", "data"))
        started = time.monotonic()
        code, fallen, summary = run_tests(base, TIMEOUT)
        print(f"[reference] {time.monotonic() - started:.0f} s", flush=True)
        print(f"[reference] code={code} {summary}", flush=True)
        if code != 0:
            print("la reference ne passe pas : arret")
            return 1
        survivors = []
        with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
            futures = [pool.submit(_try, i, m, base, tmp) for i, m in enumerate(mutations)]
            # Reported in the list's order, whatever finishes first.
            for mutation, future in zip(mutations, futures):
                line, survived = future.result()
                print(line, flush=True)
                if survived:
                    survivors.append(mutation[0])
        print()
        print(f"{len(mutations)} mutations, survivantes :", survivors or "aucune")
        return 1 if survivors else 0


if __name__ == "__main__":
    sys.exit(main())
