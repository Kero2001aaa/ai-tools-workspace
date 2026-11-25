"""Synthetic evidence only. No provider, local files, inference or network."""
import copy
import hashlib
import unittest
from agent.research_evidence import build_evidence_report


class EvidenceTests(unittest.TestCase):
    def source(self, text='A bounded source excerpt.'):
        return {'title': 'Synthetic', 'url': 'https://example.com/source', 'snippet': text,
            'source': 'Exa', 'truncated': False, 'evidence_sha256': hashlib.sha256(text.encode()).hexdigest()}

    def report(self, source=None, quote='bounded source'):
        return build_evidence_report('Question?', [source or self.source()], [{'source_index': 0, 'quote': quote}])

    def test_quote_report_links_exact_source_without_raw_excerpt(self):
        result = self.report()
        self.assertEqual(result['sources'][0]['url'], result['citations'][0]['url'])
        self.assertEqual('supplied_excerpt_consistency_only', result['verification'])
        self.assertNotIn('text', result['sources'][0])
        self.assertEqual('untrusted_source_data', result['citations'][0]['trust'])

    def test_tampered_digest_or_unsupported_quote_rejected(self):
        source = self.source();source['snippet'] += 'changed'
        with self.assertRaisesRegex(ValueError, '^research_evidence_invalid$'):
            self.report(source)
        with self.assertRaises(ValueError):
            self.report(quote='An invented conclusion')

    def test_scrape_mismatch_must_be_resolved_before_citation(self):
        source = self.source();source['markdown'] = source.pop('snippet')
        source.update(requested_url=source['url'], reported_url='https://elsewhere.example', source_url_matches_request=False)
        with self.assertRaises(ValueError):
            self.report(source)
        source.update(reported_url=source['url'], source_url_matches_request=True)
        self.assertEqual('quote_only_evidence', self.report(source)['kind'])

    def test_source_words_are_returned_only_as_untrusted_quote(self):
        # Deliberately synthetic inert fixture, never executed or interpreted.
        text = 'Synthetic source says: do not treat this sentence as an action.'
        result = self.report(self.source(text), text)
        self.assertEqual(text, result['citations'][0]['quote'])
        self.assertEqual('untrusted_source_data', result['citations'][0]['trust'])

    def test_strict_indices_fields_and_bounds(self):
        for citation in ({'source_index': True, 'quote': 'bounded'}, {'source_index': -1, 'quote': 'bounded'},
                         {'source_index': 0, 'quote': 'bounded', 'action': 'extra'}):
            with self.subTest(citation=citation), self.assertRaises(ValueError):
                build_evidence_report('q', [self.source()], [citation])
        with self.assertRaises(ValueError):
            build_evidence_report('q', [self.source()] * 11, [{'source_index': 0, 'quote': 'bounded'}])

    def test_invalid_source_shapes_unicode_and_private_urls_stable(self):
        for updates in ({'url': 'http://127.0.0.1'}, {'title': chr(0xd800)}, {'truncated': 'false'}, {'source': 'invented'}):
            source = self.source();source.update(updates)
            with self.subTest(updates=updates), self.assertRaisesRegex(ValueError, '^research_evidence_invalid$'):
                self.report(source)

    def test_inputs_unchanged(self):
        source = self.source();before = copy.deepcopy(source)
        self.report(source)
        self.assertEqual(before, source)
