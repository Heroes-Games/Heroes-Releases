"""Publish verified standalone players without changing the legacy catalog."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import urllib.error

from sync_catalog import GitHub, DESTINATION, MAX_ARCHIVE, asset, entry_for, require, verify_zip, version

GAMES = {'fighter': ('fighter', 'FighterHeroes', 'FighterHeroes'),
         'league': ('league', 'LeagueOfHeroes', 'LeagueOfHeroes')}
CATALOG = 'catalog-additional.json'


def validate(manifest):
    game = GAMES.get(manifest.get('id'))
    require(game is not None, 'Unknown additional game')
    version(manifest['version'])
    require(manifest.get('name') == game[1] and manifest.get('platform') == 'Windows x64', 'Wrong player identity')
    require(manifest.get('archive') == f"{game[2]}-v{manifest['version']}-Windows-x64.zip", 'Wrong archive name')
    require(re.fullmatch('[a-f0-9]{40}', manifest.get('commit', '')), 'Missing source provenance')
    require(manifest.get('coreTests') == 'passed' and type(manifest.get('integrationChecks')) is int and manifest['integrationChecks'] > 0
            and manifest.get('runtimeErrors') == 0 and manifest.get('playerExitCode') == 0, 'Player verification failed')
    require(type(manifest.get('size')) is int and 0 < manifest['size'] <= MAX_ARCHIVE
            and re.fullmatch('[a-f0-9]{64}', manifest.get('sha256', '')), 'Invalid archive size or checksum')
    return game


def merge(catalog, entries):
    require(catalog.get('schemaVersion') == 1 and isinstance(catalog.get('games'), list), 'Invalid catalog')
    current = catalog['games']
    ids = [g['id'] for g in current]
    require(len(ids) == len(set(ids)) and set(ids) <= GAMES.keys(), 'Only additional games belong in this catalog')
    values = {g['id']: g for g in current}
    for entry in entries:
        require(entry['id'] in GAMES, 'Unknown game')
        previous = values.get(entry['id'])
        if previous:
            if version(previous['version']) > version(entry['version']):
                continue
            if version(previous['version']) == version(entry['version']):
                require(previous == entry, 'Published version is immutable')
        values[entry['id']] = entry
    return {'schemaVersion': 1, 'games': [values[key] for key in GAMES if key in values]}


def update_catalog(api, entries):
    path = f'/repos/{DESTINATION}/contents/{CATALOG}'
    for attempt in range(8):
        current = api.request(path + '?ref=main', missing=True)
        catalog = json.loads(base64.b64decode(current['content']).decode('utf-8-sig')) if current else {'schemaVersion': 1, 'games': []}
        merged = merge(catalog, entries)
        if merged == catalog:
            return
        body = {'branch': 'main', 'message': 'Publish verified additional Heroes games',
                'content': base64.b64encode((json.dumps(merged, ensure_ascii=False, indent=2) + '\n').encode()).decode()}
        if current:
            body['sha'] = current['sha']
        try:
            api.request(path, 'PUT', body)
            return
        except urllib.error.HTTPError as error:
            if error.code not in (409, 422) or attempt == 7:
                raise
            time.sleep(min(2 ** attempt, 16))


def credential():
    token = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
    if token:
        return token
    result = subprocess.run(['git', 'credential', 'fill'], input='protocol=https\nhost=github.com\n\n',
                            text=True, encoding='utf-8', capture_output=True, check=True)
    return next(line.split('=', 1)[1] for line in result.stdout.splitlines() if line.startswith('password='))


def publish(roots):
    prepared = []
    for root in roots:
        root = Path(root)
        manifest = json.loads((root / 'release-manifest.json').read_text(encoding='utf-8-sig'))
        game = validate(manifest)
        archive = root / manifest['archive']
        with archive.open('rb') as stream:
            require(archive.stat().st_size == manifest['size'] and hashlib.file_digest(stream, 'sha256').hexdigest() == manifest['sha256'], 'Local archive changed')
        verify_zip(archive, game, manifest)
        checksum = Path(str(archive) + '.sha256')
        require(checksum.read_text(encoding='utf-8-sig').strip() == f"{manifest['sha256']}  {archive.name}", 'Wrong checksum file')
        prepared.append((root, manifest, game, archive, checksum))
    require(len({m['id'] for _, m, _, _, _ in prepared}) == len(prepared), 'Repeated game')
    api, public = GitHub(credential()), GitHub(None)
    repo = api.request(f'/repos/{DESTINATION}')
    require(repo['full_name'] == DESTINATION and not repo['private'] and repo['default_branch'] == 'main', 'Unexpected distribution destination')
    entries = []
    for root, manifest, game, archive, checksum in prepared:
        tag = f"{game[0]}-v{manifest['version']}"
        release = api.request(f'/repos/{DESTINATION}/releases/tags/{tag}', missing=True)
        if release is None:
            release = next((r for r in api.releases(DESTINATION) if r['tag_name'] == tag), None)
        if release is None:
            details = ('Duel sans armes, entraînement et duel local. Esquive, parade et attaques chargées.' if game[0] == 'fighter'
                       else 'Neuf champions, quatre compétences chacun, entraînement, duel contre le bot et duel local.')
            release = api.request(f'/repos/{DESTINATION}/releases', 'POST', {
                'tag_name': tag, 'target_commitish': 'main', 'name': f"{game[1]} {manifest['version']}", 'draft': True, 'prerelease': False,
                'body': f"{details}\n\nPrototype Windows x64. Première distribution depuis le client Heroes ; le jeu peut encore afficher le suffixe local de son build d’origine. Aucun mode réseau.\n\nTests Core réussis et {manifest['integrationChecks']} contrôles réussis dans le joueur extrait du ZIP.\n\nConserver l’exécutable avec toutes ses DLL et ses dossiers. Unity n’est pas requis.\n\nSHA-256 : `{manifest['sha256']}`"})
        require(release['tag_name'] == tag and not release['prerelease'], 'Unexpected release')
        for path in (archive, checksum):
            api.upload(DESTINATION, release, path)
        if release['draft']:
            api.request(f"/repos/{DESTINATION}/releases/{release['id']}", 'PATCH', {'draft': False, 'make_latest': 'false'})
        visible = public.request(f'/repos/{DESTINATION}/releases/tags/{tag}')
        item = asset(visible, archive.name)
        require(item['digest'] == 'sha256:' + manifest['sha256'] and item['size'] == manifest['size'], 'Public archive mismatch')
        with tempfile.TemporaryDirectory(prefix='heroes-additional-') as temporary:
            public.download(DESTINATION, item, Path(temporary) / 'verified.zip', MAX_ARCHIVE)
        entries.append(entry_for(game, manifest))
        print(f"{game[1]} {manifest['version']}: public download verified", flush=True)
    update_catalog(api, entries)
    print('Additional catalog updated; legacy catalog unchanged.', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--publish', action='store_true', required=True)
    parser.add_argument('release_directories', nargs='+')
    args = parser.parse_args()
    try:
        publish(args.release_directories)
    except urllib.error.HTTPError as error:
        raise SystemExit(f'GitHub request failed (HTTP {error.code}); retry without replacing published assets.') from None
