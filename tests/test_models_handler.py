# encoding:utf-8
import json
import os
import sys
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# 使用真实框架，避免全局 Stub 污染其他用例的网络边界。
import web


class TestModelsHandler(unittest.TestCase):
    def test_set_asr_capability_persists_provider_and_model(self):
        from channel.web.web_channel import ModelsHandler

        local_config = {}
        file_config = {}
        handler = ModelsHandler()

        with patch("channel.web.web_channel.conf", return_value=local_config):
            with patch.object(ModelsHandler, "_read_file_config", return_value=file_config):
                with patch.object(ModelsHandler, "_write_file_config") as write_file:
                    with patch.object(ModelsHandler, "_refresh_voice_routing") as refresh_voice:
                        result = json.loads(handler._handle_set_capability({
                            "capability": "asr",
                            "provider_id": "dashscope",
                            "model": "qwen3-asr-flash",
                        }))

        self.assertEqual(result["status"], "success")
        self.assertEqual(local_config["voice_to_text"], "dashscope")
        self.assertEqual(local_config["voice_to_text_model"], "qwen3-asr-flash")
        self.assertEqual(file_config["voice_to_text"], "dashscope")
        self.assertEqual(file_config["voice_to_text_model"], "qwen3-asr-flash")
        write_file.assert_called_once_with(file_config)
        refresh_voice.assert_called_once()

    def test_set_asr_empty_model_keeps_existing(self):
        # Switching provider with an empty model must not wipe a user's
        # hand-configured voice_to_text_model.
        from channel.web.web_channel import ModelsHandler

        local_config = {"voice_to_text_model": "qwen3-asr-flash"}
        file_config = {"voice_to_text_model": "qwen3-asr-flash"}
        handler = ModelsHandler()

        with patch("channel.web.web_channel.conf", return_value=local_config):
            with patch.object(ModelsHandler, "_read_file_config", return_value=file_config):
                with patch.object(ModelsHandler, "_write_file_config"):
                    with patch.object(ModelsHandler, "_refresh_voice_routing"):
                        result = json.loads(handler._handle_set_capability({
                            "capability": "asr",
                            "provider_id": "zhipu",
                            "model": "",
                        }))

        self.assertEqual(result["status"], "success")
        self.assertEqual(local_config["voice_to_text"], "zhipu")
        # Existing model preserved, not overwritten with "".
        self.assertEqual(local_config["voice_to_text_model"], "qwen3-asr-flash")
        self.assertEqual(file_config["voice_to_text_model"], "qwen3-asr-flash")
        self.assertEqual(result["model"], "qwen3-asr-flash")

    def test_asr_capability_exposes_provider_models(self):
        from channel.web.web_channel import ModelsHandler

        cap = ModelsHandler._asr_capability({
            "voice_to_text": "dashscope",
            "voice_to_text_model": "qwen3-asr-flash",
        })

        self.assertTrue(cap["editable"])
        self.assertEqual(cap["current_provider"], "dashscope")
        self.assertEqual(cap["current_model"], "qwen3-asr-flash")
        self.assertIn("provider_models", cap)
        self.assertIn("dashscope", cap["provider_models"])


if __name__ == "__main__":
    unittest.main()
