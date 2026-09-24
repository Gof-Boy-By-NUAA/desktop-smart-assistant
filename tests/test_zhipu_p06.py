import unittest
from unittest.mock import Mock
from models.zhipuai.zhipuai_bot import ZHIPUAIBot

class ZhipuP06Tests(unittest.TestCase):
    def params(self, model, **kwargs):
        bot=ZHIPUAIBot.__new__(ZHIPUAIBot)
        bot.args={'model':model}
        bot._handle_sync_response=Mock(side_effect=lambda p:p)
        return bot.call_with_tools([{'role':'user','content':'test'}], stream=False, **kwargs)

    def test_reasoning_only_models_use_low_when_deep_thinking_disabled(self):
        for model in ('glm-5.3','glm-5.3-flash','glm-5.3-flashx'):
            with self.subTest(model=model):
                p=self.params(model,thinking={'type':'disabled'})
                self.assertEqual(p['thinking']['type'],'enabled')
                self.assertEqual(p['reasoning_effort'],'low')

    def test_explicit_effort_and_thinking_options_preserved(self):
        for effort in ('low','high','max'):
            p=self.params('glm-5.3',thinking={'type':'enabled','clear_thinking':False},reasoning_effort=effort)
            self.assertEqual(p['reasoning_effort'],effort)
            self.assertFalse(p['thinking']['clear_thinking'])

    def test_old_and_unknown_models_are_not_assumed_reasoning_only(self):
        for model in ('glm-5.2','my-custom-model','glm-5.3-custom'):
            p=self.params(model,thinking={'type':'disabled'})
            self.assertEqual(p['thinking']['type'],'disabled')
            self.assertNotIn('reasoning_effort',p)

if __name__=='__main__':unittest.main()
