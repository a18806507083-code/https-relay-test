import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import identity_followup as tracker


class IdentityRules(unittest.TestCase):
    def report(self, fields):
        return '**官网 / X / Token**\n' + fields + '\n\n**价值判断**\n说明\nWATCH'

    def test_only_all_missing(self):
        self.assertTrue(tracker.all_missing(self.report('官网：未知\nX：未知\n未知')))
        self.assertTrue(tracker.all_missing(self.report('官网：未知；X：未知；Token：未知。')))
        self.assertTrue(tracker.all_missing(self.report('- 官网 / X：未知（未见官方入口）\n- Token：未知')))
        self.assertTrue(tracker.all_missing(self.report('官网：未知\nX：未知\n没发币')))

    def test_any_known_excludes(self):
        for fields in ('官网：https://project.org\nX：未知\n未知',
                       '官网：未知\nX：https://x.com/project\n未知',
                       '官网：未知\nX：@project\n未知',
                       '官网：未知\nX：未知\nCA: 0x' + '1'*40):
            self.assertFalse(tracker.all_missing(self.report(fields)))

    def test_ambiguous_fields_do_not_guess(self):
        self.assertFalse(tracker.all_missing('WATCH'))
        self.assertFalse(tracker.all_missing(self.report('官网：未知\nX：未知')))

    def test_personal_links_elsewhere_do_not_count(self):
        report = '**团队/历史**\n作者：https://x.com/developer\n' + self.report('官网：未知\nX：未知\n未知')
        self.assertTrue(tracker.all_missing(report))

    def test_oldest_report_controls_duplicate_repository(self):
        prs = [{'number': 1, 'title': '[RH-FAST][WATCH] team/project'},
               {'number': 2, 'title': '[RH-FAST][PUSH] team/project'}]
        state = {'registered': {}, 'projects': {}}
        with patch.object(tracker, 'pages', return_value=iter(prs)), patch.object(tracker, 'first_report', side_effect=[self.report('官网：https://p.org\nX：未知\n未知'), self.report('官网：未知\nX：未知\n未知')]):
            tracker.register(state)
        self.assertEqual(state['projects']['repo:team/project']['status'], 'excluded')

    def test_skip_never_registered(self):
        state = {'registered': {}, 'projects': {}}
        with patch.object(tracker, 'pages', return_value=iter([{'number': 1, 'title': '[RH-FAST][SKIP] team/project'}])), patch.object(tracker, 'first_report') as report:
            tracker.register(state)
            report.assert_not_called()
        self.assertFalse(state['projects'])

    def test_exact_support_required(self):
        docs = {'README.md': {'text': 'Official project website: https://project.org'}}
        identity = {'kind': 'website', 'value': 'https://project.org', 'source': 'README.md',
                    'quote': docs['README.md']['text'], 'official_project': True}
        self.assertTrue(tracker.validate(identity, docs))
        self.assertFalse(tracker.validate(dict(identity, value='https://invented.org'), docs))
        self.assertFalse(tracker.validate(dict(identity, official_project=False), docs))

    def test_token_requires_mainnet_and_chain_check(self):
        ca = '0x' + '1'*40
        docs = {'deploy.json': {'text': 'Our mainnet token: ' + ca}}
        identity = {'kind': 'ca', 'value': ca, 'source': 'deploy.json', 'quote': docs['deploy.json']['text'],
                    'official_project': True, 'mainnet_token': False, 'chain_id': 4663}
        with patch.object(tracker, 'token_live', return_value=True) as live:
            self.assertFalse(tracker.validate(identity, docs)); live.assert_not_called()
            self.assertTrue(tracker.validate(dict(identity, mainnet_token=True), docs))
        with patch.object(tracker, 'token_live', return_value=False):
            self.assertFalse(tracker.validate(dict(identity, mainnet_token=True), docs))

    def test_x_share_url_rejected(self):
        value = 'https://x.com/share'
        docs = {'README.md': {'text': value}}
        self.assertFalse(tracker.validate({'kind': 'x', 'value': value, 'source': 'README.md', 'quote': value, 'official_project': True}, docs))

    def test_notification_idempotent_after_state_write_failure(self):
        comments = [{'body': '<!-- RADAR-IDENTITY-FOUND-v1 -->', 'user': {'login': 'github-actions[bot]'}}]
        with patch.object(tracker, 'pages', return_value=iter(comments)), patch.object(tracker, 'gh') as gh:
            tracker.notify({'pr': 1, 'name': 'team/project'}, [])
            gh.assert_not_called()

    def test_changed_identity_fingerprint_ignores_unrelated_code(self):
        docs = {'README.md': {'text': 'hello\nno links', 'url': 'https://github.com/team/project'}}
        self.assertEqual(tracker.snippets(docs), {})
        docs['README.md']['text'] += '\nOfficial website: https://project.org'
        self.assertTrue(tracker.snippets(docs))

    def test_pagination(self):
        with patch.object(tracker, 'gh', side_effect=[[{}]*100, [{'number': 101}]]) as gh:
            self.assertEqual(len(list(tracker.pages('/pulls?state=all'))), 101)
            self.assertIn('page=2', gh.call_args[0][0])


if __name__ == '__main__':
    unittest.main()
