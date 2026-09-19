"""Mirror verified Windows game releases, then atomically advance the Heroes feed.

No private source tree, release notes, test logs or client archive are published.
SOURCE_TOKEN reads only the calling game; DISTRIBUTION_TOKEN writes Heroes-Releases.
"""
import argparse
import base64
import hashlib
import http.client
import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

DESTINATION = 'Heroes-Games/Heroes-Releases'
COMPATIBILITY = 'Nayir/Heroes-Releases'
GAMES = {
    'Heroes-Games/BrawlHeroes-Unity': ('brawl', 'BrawlHeroes', 'BrawlHeroesX'),
    'Heroes-Games/MoveHeroesX-Unity': ('move', 'MoveHeroes', 'MoveHeroesX'),
    'Heroes-Games/RogueHeroes-Unity': ('rogue', 'RogueHeroes', 'RogueHeroes'),
}
MAX_ARCHIVE = 2 * 1024**3


def require(condition, message):
    if not condition:
        raise ValueError(message)


def transient(error):
    if isinstance(error, urllib.error.HTTPError):
        return error.code in (429, 500, 502, 503, 504)
    return isinstance(error, (urllib.error.URLError, TimeoutError, ConnectionError, http.client.IncompleteRead))


def version(value):
    require(isinstance(value, str) and re.fullmatch(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)', value), 'Invalid stable version')
    parts = tuple(map(int, value.split('.')))
    require(all(x <= 2147483647 for x in parts), 'Version exceeds client limits')
    return parts


def latest_release(releases):
    stable = [r for r in releases if not r['draft'] and not r['prerelease'] and re.fullmatch(r'v\d+\.\d+\.\d+', r['tag_name'])]
    require(stable, 'No published stable game release')
    return max(stable, key=lambda r: version(r['tag_name'][1:]))


def asset(release, name):
    matches = [a for a in release['assets'] if a['name'] == name]
    require(len(matches) == 1, f'Missing or duplicate release asset: {name}')
    found = matches[0]
    require(found['state'] == 'uploaded' and isinstance(found['id'], int), f'Incomplete asset: {name}')
    require(re.fullmatch(r'sha256:[a-f0-9]{64}', found.get('digest') or ''), f'Missing GitHub digest: {name}')
    return found


def validate_manifest(game, release, manifest):
    _, _, player = game
    ver = release['tag_name'][1:]
    version(ver)
    require(not release['draft'] and not release['prerelease'], 'Only published stable releases may enter the catalog')
    require(manifest.get('version') == ver and manifest.get('tag') == 'v' + ver, 'Manifest version mismatch')
    require(manifest.get('archive') == f'{player}-v{ver}-Windows-x64.zip', 'Unexpected Windows archive')
    require(manifest.get('platform') == 'Windows x64', 'Unexpected platform')
    require(re.fullmatch(r'[a-f0-9]{40}', manifest.get('commit', '')), 'Missing commit provenance')
    shared = manifest.get('sharedPackage', {})
    require(shared.get('name') == 'com.nayir.heroes.shared' and re.fullmatch(r'[a-f0-9]{40}', shared.get('commit', '')), 'Missing shared package provenance')
    require(manifest.get('coreTests') == 'passed' and type(manifest.get('integrationChecks')) is int and manifest['integrationChecks'] > 0
            and type(manifest.get('runtimeErrors')) is int and manifest['runtimeErrors'] == 0
            and type(manifest.get('playerExitCode')) is int and manifest['playerExitCode'] == 0, 'Game verification failed or incomplete')
    require(type(manifest.get('size')) is int and 0 < manifest['size'] <= MAX_ARCHIVE, 'Invalid archive size')
    require(re.fullmatch(r'[a-f0-9]{64}', manifest.get('sha256', '')), 'Invalid archive checksum')
    archive = asset(release, manifest['archive'])
    require(archive['size'] == manifest['size'] and archive['digest'] == 'sha256:' + manifest['sha256'], 'Manifest does not match published archive')
    return archive


def entry_for(game, manifest):
    game_id, _, player = game
    return dict(id=game_id, version=manifest['version'],
                url=f"https://github.com/{COMPATIBILITY}/releases/download/{game_id}-v{manifest['version']}/{manifest['archive']}",
                sha256=manifest['sha256'], size=manifest['size'], executable=f'{player}/{player}.exe')


def merge_catalog(catalog, entry):
    require(catalog.get('schemaVersion') == 1 and isinstance(catalog.get('games'), list), 'Invalid catalog schema')
    ids = [g['id'] for g in catalog['games']]
    require(len(ids) == len(set(ids)) and set(ids) <= {g[0] for g in GAMES.values()}, 'Invalid catalog game IDs')
    current = next((g for g in catalog['games'] if g['id'] == entry['id']), None)
    if current:
        if version(current['version']) > version(entry['version']):
            return None  # A slower/older publication must never downgrade the feed.
        if current['version'] == entry['version']:
            require(current == entry, 'Published version is immutable; catalog metadata conflicts')
            return None
    updated = dict(catalog)
    updated['games'] = [entry if g['id'] == entry['id'] else g for g in catalog['games']]
    if not current:
        updated['games'].append(entry)
    return updated


def verify_zip(path, game, manifest):
    player = game[2]
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        require(len(infos) <= 100000 and sum(i.file_size for i in infos) <= 8 * 1024**3, 'Expanded archive is too large')
        require(len({n.casefold() for n in names}) == len(names), 'Duplicate Windows archive paths')
        for info in infos:
            name = info.filename.rstrip('/')
            require(name and not name.startswith('/') and '\\' not in name and ':' not in name and
                    all(part not in ('', '.', '..') for part in name.split('/')), 'Unsafe archive path')
            require((info.external_attr >> 16) & 0o170000 != 0o120000, 'Archive symlink rejected')
        for name in (f'{player}/{player}.exe', f'{player}/UnityPlayer.dll', f'{player}/{player}_Data/globalgamemanagers', f'{player}/build-info.json'):
            require(name in names, f'Incomplete Unity player: {name}')
        info = archive.getinfo(f'{player}/build-info.json')
        require(info.file_size <= 65536, 'Invalid embedded provenance')
        build = json.loads(archive.read(info).decode('utf-8-sig'))
        require(all(build.get(key) == manifest.get(key) for key in ('version', 'commit', 'platform', 'sharedPackage')), 'Embedded build does not match verified release')
        require(archive.testzip() is None, 'Corrupt ZIP entry')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GitHub:
    def __init__(self, token):
        self.token = token
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, path, method='GET', body=None, missing=False):
        for attempt in range(4):
            try:
                return self._request(path, method, body, missing)
            except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.IncompleteRead) as exc:
                if method != 'GET' or not transient(exc) or attempt == 3:
                    raise
                time.sleep(2**attempt)

    def _request(self, path, method='GET', body=None, missing=False):
        url = 'https://api.github.com' + path
        data = json.dumps(body).encode() if body is not None else None
        try:
            with self.opener.open(self.make_request(url, method, data), timeout=120) as response:
                raw = response.read(4 * 1024**2 + 1)
                require(len(raw) <= 4 * 1024**2, 'GitHub response exceeds limit')
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            if missing and exc.code == 404:
                return None
            raise

    def make_request(self, url, method='GET', data=None, accept='application/vnd.github+json'):
        require(urllib.parse.urlparse(url).scheme == 'https', 'HTTPS required')
        headers = {'Accept': accept, 'User-Agent': 'heroes-catalog-publisher', 'X-GitHub-Api-Version': '2022-11-28'}
        if self.token:
            require(urllib.parse.urlparse(url).hostname in ('api.github.com', 'uploads.github.com'), 'Refusing credential outside GitHub API')
            headers['Authorization'] = 'Bearer ' + self.token
        if data is not None:
            headers['Content-Type'] = 'application/json'
        return urllib.request.Request(url, data=data, headers=headers, method=method)

    def releases(self, repo):
        result = []
        for page in range(1, 101):
            batch = self.request(f'/repos/{repo}/releases?per_page=100&page={page}')
            result.extend(batch)
            if len(batch) < 100:
                return result
        raise ValueError('Too many release pages')

    def download(self, repo, item, path, maximum):
        require(not Path(path).exists(), 'Refusing to overwrite a download')
        for attempt in range(4):
            try:
                return self._download(repo, item, path, maximum)
            except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.IncompleteRead) as exc:
                if not transient(exc) or attempt == 3:
                    raise
                Path(path).unlink(missing_ok=True)
                time.sleep(2**attempt)

    def _download(self, repo, item, path, maximum):
        require(type(item['size']) is int and 0 < item['size'] <= maximum, 'Download exceeds size limit')
        url = f"https://api.github.com/repos/{repo}/releases/assets/{item['id']}"
        request = self.make_request(url, accept='application/octet-stream')
        for _ in range(5):
            try:
                response = self.opener.open(request, timeout=180)
                break
            except urllib.error.HTTPError as exc:
                if exc.code not in (301, 302, 303, 307, 308):
                    raise
                target = urllib.parse.urlparse(exc.headers['Location'])
                require(target.scheme == 'https' and target.hostname in ('release-assets.githubusercontent.com', 'objects.githubusercontent.com') and target.port in (None, 443) and target.username is None, 'Unexpected asset redirect')
                # Signed object URLs receive no GitHub authorization header.
                request = urllib.request.Request(target.geturl(), headers={'User-Agent': 'heroes-catalog-publisher'})
        else:
            raise ValueError('Too many asset redirects')
        digest = hashlib.sha256()
        count = 0
        with response, Path(path).open('xb') as output:
            while chunk := response.read(1024**2):
                count += len(chunk)
                require(count <= item['size'], 'Asset exceeds expected size')
                digest.update(chunk)
                output.write(chunk)
        require(count == item['size'] and 'sha256:' + digest.hexdigest() == item['digest'], 'Asset checksum or size mismatch')

    def upload(self, repo, release, path):
        path = Path(path)
        size = path.stat().st_size
        with path.open('rb') as stream:
            digest = 'sha256:' + hashlib.file_digest(stream, 'sha256').hexdigest()
        present = next((a for a in release['assets'] if a['name'] == path.name), None)
        if not present:
            require(release['draft'], 'Refusing to change published release assets')
            url = f"https://uploads.github.com/repos/{repo}/releases/{release['id']}/assets?name={urllib.parse.quote(path.name)}"
            with path.open('rb') as stream:
                request = self.make_request(url, 'POST', stream)
                request.add_header('Content-Type', 'application/octet-stream')
                request.add_header('Content-Length', str(size))
                with self.opener.open(request, timeout=1800) as response:
                    present = json.load(response)
        require(present.get('state') == 'uploaded' and present['size'] == size and present.get('digest') == digest, 'Destination asset differs; refusing overwrite')


def update_catalog(api, entry, attempts=8, pause=time.sleep):
    for attempt in range(attempts):
        current = api.request(f'/repos/{DESTINATION}/contents/catalog.json?ref=main')
        catalog = json.loads(base64.b64decode(current['content']).decode('utf-8-sig'))
        merged = merge_catalog(catalog, entry)
        if merged is None:
            return False
        content = (json.dumps(merged, ensure_ascii=False, indent=2) + '\n').encode()
        try:
            api.request(f'/repos/{DESTINATION}/contents/catalog.json', 'PUT', {
                'branch': 'main', 'sha': current['sha'],
                'message': f"Publish {entry['id']} {entry['version']} in Heroes catalog",
                'content': base64.b64encode(content).decode()})
            return True
        except urllib.error.HTTPError as exc:
            if exc.code not in (409, 422) or attempt == attempts - 1:
                raise
            # Another game may have advanced the same file. Read and merge again.
            pause(min(2**attempt, 16))


def sync(source_repo, source, destination, public=None):
    require(source_repo in GAMES, 'Only the three approved game repositories are supported')
    public = public or GitHub(None)
    game = GAMES[source_repo]
    repository = destination.request(f'/repos/{DESTINATION}')
    require(repository['full_name'] == DESTINATION and not repository['private'] and repository['default_branch'] == 'main', 'Unexpected distribution repository')
    release = latest_release(source.releases(source_repo))
    with tempfile.TemporaryDirectory(prefix='heroes-catalog-') as temporary:
        stage = Path(temporary)
        manifest_path = stage / 'release-manifest.json'
        source.download(source_repo, asset(release, manifest_path.name), manifest_path, 65536)
        manifest = json.loads(manifest_path.read_text(encoding='utf-8-sig'))
        zip_asset = validate_manifest(game, release, manifest)
        commit = source.request(f"/repos/{source_repo}/commits/{release['tag_name']}")
        require(commit['sha'] == manifest['commit'], 'Release tag no longer points to verified commit')
        entry = entry_for(game, manifest)
        catalog_file = destination.request(f'/repos/{DESTINATION}/contents/catalog.json?ref=main')
        catalog = json.loads(base64.b64decode(catalog_file['content']).decode('utf-8-sig'))
        if merge_catalog(catalog, entry) is None:
            print(f"{game[1]}: catalog already at {entry['version']} or newer")
            return entry
        archive = stage / manifest['archive']
        checksum = stage / (manifest['archive'] + '.sha256')
        source.download(source_repo, zip_asset, archive, MAX_ARCHIVE)
        source.download(source_repo, asset(release, checksum.name), checksum, 4096)
        require(checksum.read_text(encoding='utf-8-sig').strip() == f"{manifest['sha256']}  {manifest['archive']}", 'Checksum file disagrees with verified manifest')
        verify_zip(archive, game, manifest)
        tag = f"{game[0]}-v{manifest['version']}"
        mirrored = destination.request(f'/repos/{DESTINATION}/releases/tags/{tag}', missing=True)
        if mirrored is None:
            mirrored = next((r for r in destination.releases(DESTINATION) if r['tag_name'] == tag), None)
        if mirrored is None:
            mirrored = destination.request(f'/repos/{DESTINATION}/releases', 'POST', {
                'tag_name': tag, 'target_commitish': 'main', 'name': f"{game[1]} {manifest['version']}",
                'draft': True, 'prerelease': False,
                'body': f"{game[1]} {manifest['version']} — Windows x64.\n\nArchive complète du jeu validée : tests Core réussis, {manifest['integrationChecks']} contrôles d’intégration, aucune erreur de runtime.\n\nExtraire entièrement le ZIP et conserver les DLL et dossiers avec l’exécutable. Unity n’est pas requis.\n\nSHA-256 : `{manifest['sha256']}`"})
        require(not mirrored['prerelease'] and mirrored['tag_name'] == tag, 'Unexpected destination release')
        for path in (archive, checksum):
            destination.upload(DESTINATION, mirrored, path)
        if mirrored['draft']:
            destination.request(f"/repos/{DESTINATION}/releases/{mirrored['id']}", 'PATCH', {'draft': False, 'make_latest': 'false'})
        # Prove public availability and exact bytes BEFORE advertising the archive.
        visible = public.request(f'/repos/{DESTINATION}/releases/tags/{tag}')
        require(not visible['draft'] and not visible['prerelease'], 'Mirror is not public')
        public.download(DESTINATION, asset(visible, archive.name), stage / 'public-verified.zip', MAX_ARCHIVE)
        require(asset(visible, archive.name)['digest'] == zip_asset['digest'] and asset(visible, archive.name)['size'] == zip_asset['size'], 'Public archive differs from source')
        changed = update_catalog(destination, entry)
        print(f"{game[1]} {manifest['version']}: verified public download; catalog {'updated' if changed else 'already current'}")
        return entry


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', default=os.environ.get('GITHUB_REPOSITORY'))
    args = parser.parse_args()
    require(os.environ.get('SOURCE_TOKEN') and os.environ.get('DISTRIBUTION_TOKEN'), 'Scoped source and distribution tokens are required')
    try:
        result = sync(args.source, GitHub(os.environ['SOURCE_TOKEN']), GitHub(os.environ['DISTRIBUTION_TOKEN']))
        if os.environ.get('GITHUB_STEP_SUMMARY'):
            with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as summary:
                summary.write(f"Catalog synchronized: **{result['id']} {result['version']}**.\n\n[Download]({result['url']})\n")
    except urllib.error.HTTPError as error:
        # Avoid exposing request headers, signed URLs or tokens in CI logs.
        raise SystemExit(f'GitHub request failed (HTTP {error.code}); catalog retained. Retry the workflow.') from None
    except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.IncompleteRead):
        raise SystemExit('Network transfer failed after retries; catalog retained. Retry the workflow.') from None
