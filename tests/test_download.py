"""Offline regression checks; never call CourtListener or use real credentials."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

with patch.dict(os.environ, {'CASE_COUNT': '10000', 'MAX_REQUESTS_THIS_RUN': '0'}):
    spec = importlib.util.spec_from_file_location('downloader', Path(__file__).resolve().parents[1] / 'download.py')
    downloader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(downloader)


class DownloaderTests(unittest.TestCase):
    def test_untrusted_urls_rejected_before_any_request(self):
        for url in ('http://www.courtlistener.com/api/rest/v4/search/',
                    'https://example.com/api/rest/v4/search/',
                    'https://www.courtlistener.com.evil.test/api/rest/v4/search/',
                    'https://www.courtlistener.com/profile/'):
            with self.subTest(url=url), patch.object(downloader.urllib.request, 'urlopen') as request:
                with self.assertRaises(downloader.APIError):
                    downloader.api_get(url, 'test-placeholder')
                request.assert_not_called()

    def test_html_removes_scripts_and_preserves_paragraphs(self):
        self.assertEqual(downloader.strip_html('<p>A &amp; B</p><script>secret()</script><p>C</p>'), 'A & B\n\nC')

    def test_candidate_filter(self):
        self.assertTrue(downloader.candidate('Smith v. City of Example', 'A section 1983 claim against a police officer.'))
        self.assertTrue(downloader.candidate('Smith v. Example Police Department', 'An excessive force claim.'))
        self.assertFalse(downloader.candidate('United States v. Smith', 'Police officer and section 1983.'))
        self.assertFalse(downloader.candidate('Smith v. Example Police Department', 'An unrelated licensing decision.'))

    def test_opinion_falls_back_to_usable_plain_text(self):
        body = 'Opinion text. ' * 100
        self.assertEqual(downloader.opinion_text({'html_with_citations': '<p>short</p>', 'plain_text': body}), body.strip())
        self.assertEqual(downloader.opinion_text({'plain_text': 'short'}), '')

    def test_checkpoint_and_manifest_roundtrip(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.multiple(downloader, OUTPUT=root, CHECKPOINT=root / 'progress.json', MANIFEST=root / 'manifest.jsonl'):
                progress = {'version': 2, 'query_index': 3, 'page_url': None, 'offset': 7}
                downloader.atomic_json(downloader.CHECKPOINT, progress)
                self.assertEqual(downloader.load_progress(), progress)
                downloader.save_case({'cluster_id': 123, 'caseName': 'Smith v. Police Department', 'absolute_url': '/opinion/123/'}, [(456, 'Example opinion')], 'police')
                self.assertEqual(downloader.existing_records(), {'123'})
                record = json.loads(downloader.MANIFEST.read_text())
                self.assertTrue((root / record['file']).exists())
                self.assertFalse(record['reviewed'])

    def test_429_persists_cooldown_and_retries_same_url(self):
        url = downloader.SEARCH
        error = downloader.urllib.error.HTTPError(url, 429, 'Too Many Requests', {'Retry-After': '30'}, None)
        response = unittest.mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"results": []}'
        with patch.object(downloader, 'REQUEST_COUNT', 0), patch.object(downloader, 'wait_before_request') as wait, patch.object(downloader, 'update_timing_state') as state, patch.object(downloader.time, 'time', return_value=1000), patch.object(downloader.urllib.request, 'urlopen', side_effect=[error, response]) as request:
            self.assertEqual(downloader.api_get(url, 'test-placeholder'), {'results': []})
            self.assertEqual(wait.call_count, 2)
            self.assertTrue(any(call.kwargs.get('blocked_until') == 1035 for call in state.call_args_list))
            self.assertEqual([call.args[0].full_url for call in request.call_args_list], [url, url])


if __name__ == '__main__':
    unittest.main()
