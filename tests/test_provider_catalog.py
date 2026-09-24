import unittest
from unittest.mock import patch
from models import catalog

class CatalogTests(unittest.TestCase):
    def setUp(self):
        catalog._cache.clear()
        self.meta = {'models':['old'], 'api_key_field':'key', 'api_base_key':'base', 'api_base_default':'https://example.invalid/v1'}
        self.config = {'key':'test-only-key'}

    def test_no_key_and_unsupported_preserve_recommendations(self):
        with patch.object(catalog,'_read_json') as remote:
            for provider in ('openai','deepseek','claudeAPI','gemini','qianfan','mimo','minimax','zhipu','dashscope','doubao','moonshot','linkai','custom','custom:one'):
                result=catalog.get_catalog(provider,self.meta,{},discover=True)
                self.assertEqual(result['models'],['old'])
                self.assertTrue(result['custom_model_allowed'])
            remote.assert_not_called()

    def test_discovered_ids_are_not_name_filtered_and_capabilities_unknown(self):
        with patch.object(catalog,'_read_json',return_value={'data':[{'id':'brand-new-custom'},{'id':'existing'}]}) as remote:
            result=catalog.get_catalog('openai',self.meta,self.config,discover=True)
            self.assertEqual(result['models'],['brand-new-custom','existing'])
            self.assertTrue(all(v is None for v in result['metadata'][0]['capabilities'].values()))
            self.assertEqual(catalog.get_catalog('openai',self.meta,self.config),result)
            self.assertEqual(remote.call_count,1)
            catalog.get_catalog('deepseek',self.meta,self.config,discover=True)
            self.assertEqual(remote.call_count,2)

    def test_failure_fallback_is_explicit_and_credentials_not_exposed(self):
        with patch.object(catalog,'_read_json',side_effect=ValueError('test-only-key')):
            result=catalog.get_catalog('openai',self.meta,self.config,discover=True)
        self.assertEqual(result['discovery'],'failed')
        self.assertEqual(result['models'],['old'])
        self.assertNotIn('test-only-key',str(result))

    def test_gemini_pagination_and_only_generation_models(self):
        pages=[{'models':[{'name':'models/new','supportedGenerationMethods':['generateContent'],'inputTokenLimit':123}], 'nextPageToken':'two'}, {'models':[{'name':'models/embedding','supportedGenerationMethods':['embedContent']}]}]
        with patch.object(catalog,'_read_json',side_effect=pages) as remote:
            result=catalog.get_catalog('gemini',self.meta,self.config,discover=True)
        self.assertEqual(result['models'],['new'])
        self.assertEqual(result['metadata'][0]['capabilities']['context_window'],123)
        self.assertEqual(remote.call_count,2)

    def test_invalid_endpoint_does_not_send_credentials(self):
        with patch.object(catalog,'_read_json') as remote:
            result=catalog.get_catalog('openai',self.meta,dict(self.config,base='http://example.invalid/v1'),discover=True)
        self.assertEqual(result['discovery'],'failed')
        remote.assert_not_called()

if __name__=='__main__': unittest.main()
