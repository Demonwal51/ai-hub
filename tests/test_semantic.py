"""The memory by meaning (aihub/semantic.py), with a stand-in embedding model.

The stand-in turns a text into a vector of « themes »: words of the same
theme (teinte, couleur, vert) land on the same axis, as a real model's
meaning would, so the search by meaning can be checked without Ollama.
"""

import unittest

from aihub import db, memory, semantic, store
from aihub.semantic import SemanticError

from .helpers import TempDirMixin

THEMES = [{"teinte", "couleur", "vert", "sapin", "emblème", "logo"},
          {"marges", "espacements", "px", "mobile", "téléphone"},
          {"réunion", "rendez-vous", "jeudi", "client"}]


class Themes(semantic.Embedder):
    def __init__(self):
        super().__init__("http://stand-in", "themes")
        self.calls = []
        self.down = False

    def embed(self, texts, query=False):
        if self.down:
            raise SemanticError("arrêté")
        self.calls.append((list(texts), query))
        out = []
        for text in texts:
            words = set(text.casefold().replace("?", " ").replace(".", " ").replace(",", " ").split())
            out.append(semantic._normalized([len(words & theme) + 0.01 for theme in THEMES]))
        return out


class SemanticTest(TempDirMixin, unittest.TestCase):
    def setUp(self):
        self.tmp = self.make_tempdir()
        self.db_path = str(self.tmp / "hub.db")
        self.conn = db.connect(self.db_path)
        self.addCleanup(self.conn.close)
        self.agent = store.create_agent(self.conn, "Claude", "claude_code", "haiku")
        self.cid = store.create_conversation(self.conn, "Boutique", agent_id=self.agent, workdir=str(self.tmp))
        self.model = Themes()
        semantic.set_embedder(self.model)
        self.addCleanup(semantic.set_embedder, None)

    def say(self, text, cid=None):
        cid = cid or self.cid
        store.add_user_message(self.conn, cid, text)
        mid = store.start_request(self.conn, cid, self.agent)
        store.mark_message_running(self.conn, mid)
        store.finish_message(self.conn, mid, "ok", content="noté")
        return mid

    def index(self):
        return semantic.Indexer(self.db_path).tick()

    def test_scoped_search_ranks_only_authorised_vectors(self):
        private = store.create_conversation(self.conn, "Privée", agent_id=self.agent, workdir=str(self.tmp))
        for _ in range(50):
            self.say("couleur vert sapin", cid=private)
        self.say("couleur vert sapin")
        self.index()
        rows = memory.search(self.conn, "teinte", limit=1, conversation_ids={self.cid})
        self.assertEqual([row["conversation_id"] for row in rows], [self.cid])
        self.assertTrue(rows[0]["by_meaning"])

    def test_the_vectors_are_made_once(self):
        self.say("Pour le logo, la couleur sera vert sapin.")
        self.assertEqual(self.index(), 2)  # the request and the reply
        self.assertEqual(self.index(), 0)
        self.assertTrue(all(not query for _, query in self.model.calls))  # documents, not questions

    def test_a_reply_is_indexed_only_once_finished(self):
        store.add_user_message(self.conn, self.cid, "x")
        mid = store.start_request(self.conn, self.cid, self.agent)
        store.mark_message_running(self.conn, mid)
        store.append_message_content(self.conn, mid, "une réponse à moitié écrite")
        self.assertEqual(self.index(), 1)  # the request only, not the reply being written

    def test_meaning_finds_what_words_miss(self):
        self.say("Pour le logo, la couleur sera vert sapin.")
        self.say("Les marges : 24 px, 16 px sur téléphone.")
        self.index()
        found = memory.passages(self.conn, self.cid, "quelle teinte pour l'emblème ?", 2000)
        self.assertIn("vert sapin", found[0]["content"])  # no word in common with the question
        self.assertTrue(self.model.calls[-1][1])  # asked as a question
        self.assertFalse(any("marges" in f["content"] for f in found))  # far in meaning: left out

    def test_words_and_meaning_together(self):
        self.say("Pour le logo, la couleur sera vert sapin.")
        self.say("Le logo sera en haut à gauche.")
        self.index()
        found = memory.passages(self.conn, self.cid, "le logo et sa couleur", 2000)
        self.assertIn("vert sapin", found[0]["content"])  # found by both: first

    def test_without_the_model_words_alone(self):
        self.say("Pour le logo, la couleur sera vert sapin.")
        self.index()
        self.model.down = True
        self.assertEqual(memory.passages(self.conn, self.cid, "quelle teinte pour l'emblème ?", 2000), [])
        self.assertTrue(memory.passages(self.conn, self.cid, "logo", 2000))
        self.assertEqual(self.index(), 0)  # nothing made, nothing broken

    def test_sidebar_search(self):
        other = store.create_conversation(self.conn, "Agenda", agent_id=self.agent, workdir=str(self.tmp))
        self.say("La réunion avec le client est jeudi.", cid=other)
        self.index()
        found = memory.search(self.conn, "rendez-vous")
        self.assertEqual((found[0]["title"], found[0]["by_meaning"]), ("Agenda", True))

    def test_deleted_with_the_conversation(self):
        self.say("Pour le logo, la couleur sera vert sapin.")
        self.index()
        store.delete_conversation(self.conn, self.cid)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM memory_vectors").fetchone()[0], 0)

    def test_fuse(self):
        self.assertEqual(semantic.fuse(["a", "b"], ["b", "c"]), ["b", "a", "c"])

    def test_vectors_round_trip(self):
        vector = semantic._normalized([3, 4])
        self.assertAlmostEqual(semantic.dot(vector, semantic.unpack(semantic.pack(vector))), 1.0, places=5)


if __name__ == "__main__":
    unittest.main()
