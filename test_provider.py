"""Provider adapter tests use a fake HTTP response; none performs network I/O."""
import json
import os
import sys
import unittest
from unittest import mock
from pathlib import Path

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE))
from provider import OpenAICompatibleProvider, ProviderConfig, ProviderError
import tools


class FakeResponse:
    def __init__(self, value): self.value=json.dumps(value).encode()
    def __enter__(self): return self
    def __exit__(self,*_): return False
    def read(self,n=-1): return self.value[:n]


class ProviderTests(unittest.TestCase):
    def test_request_configuration_usage_cost_and_key_not_in_result(self):
        requests=[]
        def opener(request,timeout):
            requests.append((request,timeout))
            return FakeResponse({"model":"gpt-6-luna","choices":[{"message":{"content":"answer"}}],
                                 "usage":{"prompt_tokens":1200,"completion_tokens":200}})
        config=ProviderConfig(base_url="https://example.invalid/v1",model="gpt-6-luna",
                              api_key_env="LEVEL2_TEST_KEY",timeout=9,
                              input_usd_per_million=.10,output_usd_per_million=.50)
        with mock.patch.dict(os.environ,{"LEVEL2_TEST_KEY":"never-log-this"}):
            result=OpenAICompatibleProvider(config,opener=opener).complete(
                [{"role":"user","content":"bounded prompt"}],max_tokens=500)
        request,timeout=requests[0]
        body=json.loads(request.data)
        self.assertEqual(request.full_url,"https://example.invalid/v1/chat/completions")
        self.assertEqual(request.get_header("Authorization"),"Bearer never-log-this")
        self.assertEqual(body["model"],"gpt-6-luna")
        self.assertEqual(body["max_completion_tokens"],500)
        self.assertEqual(body["reasoning_effort"],"none")
        self.assertEqual(timeout,9)
        self.assertEqual(result["text"],"answer")
        self.assertEqual((result["input_tokens"],result["output_tokens"]),(1200,200))
        self.assertAlmostEqual(result["estimated_cost_usd"],.00022)
        self.assertNotIn("never-log-this",repr(result))

    def test_missing_key_names_variable_without_exposing_a_value(self):
        config=ProviderConfig(api_key_env="LEVEL2_MISSING_TEST_KEY")
        with mock.patch.dict(os.environ,{},clear=True):
            with self.assertRaisesRegex(ProviderError,"LEVEL2_MISSING_TEST_KEY"):
                OpenAICompatibleProvider(config).complete([{"role":"user","content":"x"}])

    def test_localhost_http_allowed_but_remote_plain_http_rejected(self):
        ProviderConfig(base_url="http://127.0.0.1:8000/v1")
        with self.assertRaises(ValueError): ProviderConfig(base_url="http://example.com/v1")

    def test_worker_subprocess_environment_does_not_inherit_provider_key(self):
        class Completed:
            returncode=0; stdout=""; stderr=""
        with mock.patch.dict(os.environ,{"LEVEL2_API_KEY":"must-not-reach-worker"}), \
             mock.patch("tools.subprocess.run",return_value=Completed()) as run:
            tools._run(["/usr/bin/true"],Path("/tmp"))
        worker_env=run.call_args.kwargs["env"]
        self.assertNotIn("LEVEL2_API_KEY",worker_env)
        self.assertNotIn("must-not-reach-worker",repr(worker_env))


if __name__=='__main__': unittest.main(verbosity=2)
