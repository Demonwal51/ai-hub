#!/bin/sh
# Installation Linux sans sudo : les données restent dans ce dossier.
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
umask 077
python_cmd=${AIHUB_PYTHON:-python3}
if ! "$python_cmd" -c 'import sys; sys.exit(sys.version_info < (3, 12))' 2>/dev/null; then
    printf '%s\n' 'Python 3.12 ou plus est nécessaire.'
    printf '%s\n' 'Debian/Ubuntu : installer python3, python3-venv et python3-pip.'
    printf '%s\n' 'Fedora : installer python3 et python3-pip. Aucun sudo utilisé par ce script.'
    exit 1
fi
if [ ! -x .venv/bin/python ]; then
    "$python_cmd" -m venv .venv || {
        printf '%s\n' 'Création du venv impossible : vérifiez le paquet python3-venv.' >&2
        exit 1
    }
fi
.venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt
mkdir -p data
chmod 700 data
chmod u+x lancer.sh
if [ "${AIHUB_NO_SHORTCUT:-0}" != 1 ]; then
    .venv/bin/python -m aihub.linux desktop
fi
printf '%s\n' 'AI Hub installé. Lancez ./lancer.sh ou le raccourci AI Hub.'
