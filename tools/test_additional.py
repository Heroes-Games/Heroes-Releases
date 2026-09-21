import unittest
from publish_additional import merge, validate


class AdditionalTests(unittest.TestCase):
    def test_merge_keeps_newer_and_other_game(self):
        newer = dict(id='fighter', version='0.2.0')
        league = dict(id='league', version='0.3.3')
        result = merge(dict(schemaVersion=1, games=[newer]), [dict(id='fighter', version='0.1.4'), league])
        self.assertEqual(result['games'], [newer, league])

    def test_existing_version_cannot_change(self):
        with self.assertRaises(ValueError):
            merge(dict(schemaVersion=1, games=[dict(id='league', version='0.3.3', sha256='a')]), [dict(id='league', version='0.3.3', sha256='b')])

    def test_legacy_games_are_rejected(self):
        with self.assertRaises(ValueError):
            merge(dict(schemaVersion=1, games=[dict(id='brawl', version='0.1.0')]), [])

    def test_failed_player_is_not_publishable(self):
        manifest = dict(id='league', name='LeagueOfHeroes', version='0.3.3', platform='Windows x64',
                        archive='LeagueOfHeroes-v0.3.3-Windows-x64.zip', commit='a'*40,
                        coreTests='passed', integrationChecks=2341, runtimeErrors=0, playerExitCode=0, size=123, sha256='b'*64)
        self.assertEqual(validate(manifest)[0], 'league')
        for key, value in [('coreTests', 'failed'), ('integrationChecks', 0), ('runtimeErrors', 1), ('playerExitCode', 1)]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate({**manifest, key: value})
