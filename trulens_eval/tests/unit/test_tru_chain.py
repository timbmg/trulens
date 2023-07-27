"""
Tests for TruChain. This is outdated.
"""

import asyncio
from datetime import datetime
import os
from typing import Dict, Sequence
import unittest
from unittest import main
from unittest import TestCase

from langchain import LLMChain
from langchain import PromptTemplate
from langchain.callbacks import AsyncIteratorCallbackHandler
from langchain.chains import ConversationalRetrievalChain
from langchain.chains import LLMChain
from langchain.chains import SimpleSequentialChain
from langchain.chat_models.openai import ChatOpenAI
from langchain.embeddings.openai import OpenAIEmbeddings
from langchain.memory import ConversationBufferWindowMemory
from langchain.schema.messages import HumanMessage
from langchain.vectorstores import Pinecone
import pinecone

from trulens_eval import feedback
from trulens_eval import Feedback
from trulens_eval import Tru
from trulens_eval.keys import check_keys
from trulens_eval.provider_apis import Endpoint
from trulens_eval.provider_apis import OpenAIEndpoint
from trulens_eval.tru_chain import TruChain
from trulens_eval.util import JSON_BASES
from trulens_eval.util import JSONPath

import trulens_eval.utils.python # makes sure asyncio gets instrumented

check_keys(
    "OPENAI_API_KEY", "HUGGINGFACE_API_KEY", "PINECONE_API_KEY", "PINECONE_ENV"
)


class JSONTestCase(TestCase):

    def assertJSONEqual(
        self, j1, j2, path: JSONPath = None, skips=None
    ) -> None:
        skips = skips or set([])
        path = path or JSONPath()

        self.assertIsInstance(j1, type(j2), str(path))

        if isinstance(j1, JSON_BASES):
            self.assertEqual(j1, j2, str(path))

        elif isinstance(j1, Dict):

            ks1 = set(j1.keys())
            ks2 = set(j2.keys())

            self.assertSetEqual(ks1, ks2, str(path))

            for k in ks1:
                if k in skips:
                    continue

                self.assertJSONEqual(j1[k], j2[k], skips=skips, path=path[k])

        elif isinstance(j1, Sequence):
            self.assertEqual(len(j1), len(j2), str(path))

            for i, (v1, v2) in enumerate(zip(j1, j2)):
                self.assertJSONEqual(v1, v2, skips=skips, path=path[i])

        elif isinstance(j1, datetime):
            self.assertEqual(j1, j2, str(path))

        else:
            raise RuntimeError(
                f"Don't know how to compare objects of type {type(j1)} ."
            )


class TestTruChain(JSONTestCase):

    def setUp(self):
        # Setup of outdated tests:
        """
        self.llm_model_id = "gpt2"
        # This model is pretty bad but using it for tests because it is free and
        # relatively small.

        # model_id = "decapoda-research/llama-7b-hf"
        # model_id = "decapoda-research/llama-13b-hf"

 
        self.model = AutoModelForCausalLM.from_pretrained(
            self.llm_model_id,
            device_map='auto',
            torch_dtype=torch.float16,
            local_files_only=True
        )

        self.tokenizer = AutoTokenizer.from_pretrained(
            self.llm_model_id, local_files_only=True
        )

        self.pipe = pipeline(
            "text-generation",
            model=self.model,
            tokenizer=self.tokenizer,
            max_new_tokens=16,
            device_map="auto",
            early_stopping=True
        )

        self.llm = HuggingFacePipeline(pipeline=self.pipe)
        """

    def test_async_with_task(self):
        asyncio.run(self._async_with_task())

    async def _async_with_task(self):
        # Check whether an async call that makes use of Task (via
        # asyncio.gather) can still track costs.

        # TODO: move to a different test file as TruChain is not involved.

        msg = HumanMessage(content="Hello there")

        prompt = PromptTemplate.from_template(
            """Honestly answer this question: {question}."""
        )
        llm = ChatOpenAI(temperature=0.0, streaming=False, cache=False)
        chain = LLMChain(llm=llm, prompt=prompt)

        async def test1():
            # Does not create a task:
            result = await chain.llm._agenerate(messages=[msg])
            return result

        res1, costs1 = await Endpoint.atrack_all_costs(test1)

        async def test2():
            # Creates a task internally via asyncio.gather:
            result = await chain._acall(inputs=dict(question="hello there"))
            return result

        res2, costs2 = await Endpoint.atrack_all_costs(test2)

        # Results are not the same as they involve different prompts but should
        # not be empty at least:
        self.assertTrue(len(res1.generations[0].text) > 5)
        self.assertTrue(len(res2['text']) > 5)

        # And cost tracking should have counted some number of tokens.
        self.assertTrue(costs1[0].cost.n_tokens > 3)
        self.assertTrue(costs2[0].cost.n_tokens > 3)

    def test_async_call_with_record(self):
        asyncio.run(self._async_call_with_record())

    async def _async_call_with_record(self):
        # Check that the async acall_with_record produces the same stuff as the
        # sync call_with_record.

        OpenAIEndpoint()

        # Create simple QA chain.
        tru = Tru()
        prompt = PromptTemplate.from_template(
            """Honestly answer this question: {question}."""
        )

        message = "What is 1+2?"

        # Get sync results.
        llm = ChatOpenAI(temperature=0.0)
        chain = LLMChain(llm=llm, prompt=prompt)
        tc = tru.Chain(chain)
        sync_res, sync_record = tc.call_with_record(
            inputs=dict(question=message)
        )

        # Get async results.
        llm = ChatOpenAI(temperature=0.0)
        chain = LLMChain(llm=llm, prompt=prompt)
        tc = tru.Chain(chain)
        async_res, async_record = await tc.acall_with_record(
            inputs=dict(question=message),
        )

        self.assertJSONEqual(async_res, sync_res)

        self.assertJSONEqual(
            async_record.dict(),
            sync_record.dict(),
            skips=set(
                [
                    "cost",
                    "name",
                    "ts",
                    "start_time",
                    "end_time",
                    "record_id"
                ]
            )
        )

    def test_async_token_gen(self):
        asyncio.run(self._test_async_token_gen())

    async def _test_async_token_gen(self):
        # Test of chain acall methods as requested in https://github.com/truera/trulens/issues/309 .

        class AsyncAgent:

            def __init__(self):
                tru = Tru()
                # hugs = feedback.Huggingface()
                # f_lang_match = Feedback(hugs.language_match).on_input_output()

                self.async_callback = AsyncIteratorCallbackHandler()
                prompt = PromptTemplate.from_template(
                    """Honestly answer this question: {question}."""
                )
                llm = ChatOpenAI(
                    temperature=0.0,
                    streaming=True,
                    callbacks=[self.async_callback]
                )
                self.agent = LLMChain(llm=llm, prompt=prompt)
                self.agent = tru.Chain(self.agent)  #, feedbacks=[f_lang_match])

            async def respond_each_token(self, message):
                f_res_record = asyncio.create_task(
                    self.agent.acall_with_record(
                        inputs=dict(question=message),
                    )
                )

                async for token in self.async_callback.aiter():
                    yield token

                # Storing these in self as we are an async generator that is
                # already done.
                self.res, self.record = await f_res_record

        st = AsyncAgent()
        message = "What is 1+2? Explain your answer."

        # Get the token stream.
        async for tok in st.respond_each_token(message):
            print(tok)

        # Get the results of the async acall_with_record.
        async_res = st.res
        async_record = st.record

        # Also get the same using the sync version. Need to disable streaming first.
        st.agent.app.llm.streaming = False
        sync_res, sync_record = st.agent.call_with_record(
            inputs=dict(question=message),
        )

        self.assertJSONEqual(async_res, sync_res)

        self.assertJSONEqual(
            async_record.dict(),
            sync_record.dict(),
            skips=set(
                [
                    # "text", # output in stream mode is empty
                    # "main_output", # same
                    "cost",  # similar problem
                    "name",
                    "ts",
                    "start_time",
                    "end_time",
                    "record_id"
                ]
            )
        )

    @unittest.skip("outdated")
    def test_qa_prompt(self):
        # Test of a small q/a app using a prompt and a single call to an llm.

        # llm = OpenAI()

        template = """Q: {question} A:"""
        prompt = PromptTemplate(template=template, input_variables=["question"])
        llm_app = LLMChain(prompt=prompt, llm=self.llm)

        tru_app = TruChain(app=llm_app)

        assert tru_app.app is not None

        tru_app.run(dict(question="How are you?"))
        tru_app.run(dict(question="How are you today?"))

        assert len(tru_app.db.select()) == 2

    @unittest.skip("outdated")
    def test_qa_prompt_with_memory(self):
        # Test of a small q/a app using a prompt and a single call to an llm.
        # Also has memory.

        # llm = OpenAI()

        template = """Q: {question} A:"""
        prompt = PromptTemplate(template=template, input_variables=["question"])

        memory = ConversationBufferWindowMemory(k=2)

        llm_app = LLMChain(prompt=prompt, llm=self.llm, memory=memory)

        tru_app = TruChain(app=llm_app)

        assert tru_app.app is not None

        tru_app.run(dict(question="How are you?"))
        tru_app.run(dict(question="How are you today?"))

        assert len(tru_app.db.select()) == 2

    @unittest.skip("outdated")
    def test_qa_db(self):
        # Test a q/a app that uses a vector store to look up context to include in
        # llm prompt.

        # WARNING: this test incurs calls to pinecone and openai APIs and may cost money.

        index_name = "llmdemo"

        embedding = OpenAIEmbeddings(
            model='text-embedding-ada-002'
        )  # 1536 dims

        pinecone.init(
            api_key=os.environ.get('PINECONE_API_KEY'
                                  ),  # find at app.pinecone.io
            environment=os.environ.get('PINECONE_ENV'
                                      )  # next to api key in console
        )
        docsearch = Pinecone.from_existing_index(
            index_name=index_name, embedding=embedding
        )

        # llm = OpenAI(temperature=0,max_tokens=128)

        retriever = docsearch.as_retriever()
        app = ConversationalRetrievalChain.from_llm(
            llm=self.llm, retriever=retriever, return_source_documents=True
        )

        tru_app = TruChain(app)
        assert tru_app.app is not None
        tru_app(dict(question="How do I add a model?", chat_history=[]))

        assert len(tru_app.db.select()) == 1

    @unittest.skip("outdated")
    def test_sequential(self):
        # Test of a sequential app that contains the same llm twice with
        # different prompts.

        template = """Q: {question} A:"""
        prompt = PromptTemplate(template=template, input_variables=["question"])
        llm_app = LLMChain(prompt=prompt, llm=self.llm)

        template_2 = """Reverse this sentence: {sentence}."""
        prompt_2 = PromptTemplate(
            template=template_2, input_variables=["sentence"]
        )
        llm_app_2 = LLMChain(prompt=prompt_2, llm=self.llm)

        seq_app = SimpleSequentialChain(
            apps=[llm_app, llm_app_2],
            input_key="question",
            output_key="answer"
        )
        seq_app.run(
            question="What is the average air speed velocity of a laden swallow?"
        )

        tru_app = TruChain(seq_app)
        assert tru_app.app is not None

        # This run should not be recorded.
        seq_app.run(
            question="What is the average air speed velocity of a laden swallow?"
        )

        # These two should.
        tru_app.run(
            question=
            "What is the average air speed velocity of a laden european swallow?"
        )
        tru_app.run(
            question=
            "What is the average air speed velocity of a laden african swallow?"
        )

        assert len(tru_app.db.select()) == 2


if __name__ == '__main__':
    main()
