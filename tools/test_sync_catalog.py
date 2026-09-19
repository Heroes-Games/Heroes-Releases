import base64
import copy
import hashlib
import json
import tempfile
import unittest
import urllib.error
import zipfile
from pathlib import Path
from unittest.mock import patch

from sync_catalog import GAMES, GitHub, entry_for, latest_release, merge_catalog, sync, update_catalog, validate_manifest, verify_zip

GAME = GAMES['Heroes-Games/BrawlHeroes-Unity']
MANIFEST = dict(version='0.26.3', tag='v0.26.3', archive='BrawlHeroesX-v0.26.3-Windows-x64.zip',
                platform='Windows x64', commit='a'*40, sha256='b'*64, size=100,
                sharedPackage=dict(name='com.nayir.heroes.shared', commit='c'*40, version='0.1.0'),
                coreTests='passed', integrationChecks=10, runtimeErrors=0, playerExitCode=0)


def release(ver, draft=False, prerelease=False):
    return dict(tag_name='v'+ver, draft=draft, prerelease=prerelease, assets=[
        dict(name=MANIFEST['archive'], id=1, state='uploaded', size=100, digest='sha256:'+'b'*64)])


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.entry = entry_for(GAME, MANIFEST)
        self.old = dict(self.entry, version='0.25.1')
        self.move = dict(id='move', version='0.12.0', note='must survive another game update')
        self.catalog = dict(schemaVersion=1, games=[self.old, self.move])

    def test_highest_semantic_stable_version_not_publication_order(self):
        chosen = latest_release([release('0.9.9'), release('0.100.0', draft=True), release('0.99.0', prerelease=True), release('0.26.3'), release('0.25.1')])
        self.assertEqual(chosen['tag_name'], 'v0.26.3')

    def test_no_stable_release_is_error(self):
        with self.assertRaises(ValueError):
            latest_release([release('0.1.0', draft=True)])

    def test_new_game_preserves_other_entries(self):
        changed = merge_catalog(self.catalog, self.entry)
        self.assertEqual(changed['games'], [self.entry, self.move])
        self.assertEqual(self.catalog['games'][0], self.old)

    def test_backport_cannot_downgrade(self):
        self.assertIsNone(merge_catalog(dict(schemaVersion=1, games=[self.entry]), self.old))

    def test_retry_is_idempotent(self):
        self.assertIsNone(merge_catalog(dict(schemaVersion=1, games=[self.entry]), self.entry))

    def test_same_version_changed_hash_is_rejected(self):
        with self.assertRaises(ValueError):
            merge_catalog(dict(schemaVersion=1, games=[self.entry]), dict(self.entry, sha256='d'*64))

    def test_compatibility_urls_retained(self):
        self.assertEqual(self.entry['url'], 'https://github.com/Nayir/Heroes-Releases/releases/download/brawl-v0.26.3/BrawlHeroesX-v0.26.3-Windows-x64.zip')

    def test_invalid_and_duplicate_ids_rejected(self):
        for games in ([self.old, self.old], [dict(self.old, id='client')]):
            with self.assertRaises(ValueError):
                merge_catalog(dict(schemaVersion=1, games=games), self.entry)

    def test_verification_provenance_and_assets_required(self):
        validate_manifest(GAME, release('0.26.3'), MANIFEST)
        for key, value in [('version','0.26.2'), ('coreTests','failed'), ('runtimeErrors',1), ('playerExitCode',1), ('integrationChecks',0), ('size',101), ('sha256','d'*64), ('commit','invalid'), ('platform','Linux'), ('archive','../../client.zip')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_manifest(GAME, release('0.26.3'), dict(MANIFEST, **{key: value}))
        for data in (release('0.26.3', draft=True), release('0.26.3', prerelease=True)):
            with self.assertRaises(ValueError):
                validate_manifest(GAME, data, MANIFEST)

    def test_retry_merges_concurrent_game_without_lost_update(self):
        testcase = self
        class API:
            attempts = 0
            def request(self, path, method='GET', body=None):
                if method == 'GET':
                    catalog = copy.deepcopy(testcase.catalog)
                    if self.attempts:
                        catalog['games'][1]['version'] = '0.13.0'
                    return dict(sha=str(self.attempts), content=base64.b64encode(json.dumps(catalog).encode()).decode())
                self.attempts += 1
                if self.attempts == 1:
                    raise urllib.error.HTTPError('https://api.github.com', 409, 'Conflict', {}, None)
                self.saved = json.loads(base64.b64decode(body['content']))
        api = API()
        self.assertTrue(update_catalog(api, self.entry, pause=lambda _: None))
        self.assertEqual(api.saved['games'][0], self.entry)
        self.assertEqual(api.saved['games'][1]['version'], '0.13.0')

    def test_race_newer_same_game_wins(self):
        testcase = self
        class API:
            writes = 0
            def request(self, path, method='GET', body=None):
                if method == 'PUT':
                    self.writes += 1
                    raise urllib.error.HTTPError('https://api.github.com', 409, 'Conflict', {}, None)
                game = dict(testcase.entry, version='0.27.0') if self.writes else testcase.old
                return dict(sha='s', content=base64.b64encode(json.dumps(dict(schemaVersion=1,games=[game])).encode()).decode())
        api = API()
        self.assertFalse(update_catalog(api, self.entry, pause=lambda _: None))
        self.assertEqual(api.writes, 1)

    def test_credential_never_sent_to_asset_host(self):
        with self.assertRaises(ValueError):
            GitHub('test-token').make_request('https://release-assets.githubusercontent.com/file')

    def test_release_paging_does_not_skip_older_publication_of_newer_version(self):
        api = GitHub(None)
        api.request = lambda path: [release('0.1.0')]*100 if 'page=1' in path.split('&')[-1] else [release('0.26.3')]
        self.assertEqual(latest_release(api.releases('owner/repo'))['tag_name'], 'v0.26.3')

    def test_zip_valid_provenance_and_hostile_path(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'game.zip'
            with zipfile.ZipFile(path,'w') as z:
                for name in ('BrawlHeroesX.exe','UnityPlayer.dll','BrawlHeroesX_Data/globalgamemanagers'):
                    z.writestr('BrawlHeroesX/'+name,b'fixture')
                z.writestr('BrawlHeroesX/build-info.json',json.dumps(MANIFEST))
            verify_zip(path,GAME,MANIFEST)
            with self.assertRaises(ValueError):
                verify_zip(path,GAME,dict(MANIFEST,commit='d'*40))
            with zipfile.ZipFile(path,'a') as z:
                z.writestr('../outside.exe',b'hostile')
            with self.assertRaises(ValueError):
                verify_zip(path,GAME,MANIFEST)

    def test_corrupt_download_never_reaches_publication(self):
        manifest = copy.deepcopy(MANIFEST)
        manifest_data = json.dumps(manifest).encode()
        source_release = release('0.26.3')
        source_release['assets'].append(dict(name='release-manifest.json', id=2, state='uploaded', size=len(manifest_data), digest='sha256:'+hashlib.sha256(manifest_data).hexdigest()))
        testcase = self
        class Source:
            def releases(self, _): return [source_release]
            def request(self, _): return dict(sha=manifest['commit'])
            def download(self, repo, item, path, maximum):
                if item['name'] == 'release-manifest.json': Path(path).write_bytes(manifest_data)
                else: raise ValueError('Asset checksum or size mismatch')
        class Destination:
            writes = []
            def request(self, path, method='GET', body=None, **kwargs):
                if method != 'GET': self.writes.append(method)
                if '/contents/' in path:
                    return dict(content=base64.b64encode(json.dumps(testcase.catalog).encode()).decode())
                return dict(full_name='Heroes-Games/Heroes-Releases', private=False, default_branch='main')
        dest = Destination()
        with self.assertRaisesRegex(ValueError, 'checksum'):
            sync('Heroes-Games/BrawlHeroes-Unity', Source(), dest)
        self.assertEqual(dest.writes, [])


if __name__ == '__main__':
    unittest.main()
