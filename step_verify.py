import functools
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk import trace as trace_sdk
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry import trace as trace_api
from openinference.instrumentation.dspy import DSPyInstrumentor
import phoenix
import re
import torch
import tqdm
from enum import Enum
from typing import Annotated, Protocol, Tuple, Optional
import dspy
from dspy.predict import Retry
from dspy.functional import TypedChainOfThought
from dspy.primitives.assertions import assert_transform_module, backtrack_handler
import typer
from transformers import AutoTokenizer, AutoModelForSequenceClassification, AutoModel
from concurrent.futures import ThreadPoolExecutor

# TODO: Use Chat Adapters from dspy instead of manually formatting the chat

app = typer.Typer()

DEVICE = (
    "cuda"
    if torch.cuda.is_available()
    else "mps"
    if torch.backends.mps.is_available()
    else "cpu"
)


phoenix.launch_app(host="localhost", port=6006)
tracer_provider = trace_sdk.TracerProvider()
tracer_provider.add_span_processor(
    SimpleSpanProcessor(
        span_exporter=OTLPSpanExporter(endpoint="http://localhost:6006/v1/traces")
    )
)
trace_api.set_tracer_provider(tracer_provider=tracer_provider)
DSPyInstrumentor().instrument()


class LanguageModel(str, Enum):
    M_0_5 = "qwen:0.5b"
    M_1_8 = "qwen:latest"
    M_7_A = "meditron:7b"
    M_7_B = "mistral:v0.2"
    M_13 = "mixtral:latest"
    M_35 = "command-r:latest"
    M_70 = "meditron:70b"


class StepAnnotation(str, Enum):
    ESSENTIAL_AND_VALID = "essential_valid"
    UNNECESSARY = "unnecessary"
    LOGICALLY_FALSE = "logically_false"
    NOT_BACKED_BY_PRIOR_FACTS = "not_backed_by_prior_facts"
    BAD_DEDUCTIVE_REASONING = "bad_deductive_reasoning"
    DOES_NOT_SEEM_RIGHT = "does_not_seem_right"


class StepVerfication(dspy.Signature):
    objective: str = dspy.InputField()
    chat_history: str = dspy.InputField(
        desc="Entire Chat history with the latest message from the user as the bottom"
    )
    reasoning_chain: str = dspy.InputField(
        desc="The reasoning chain generated by the AI system in order to respond to the user's chat given their objectives"
    )
    step_to_be_verified: str = dspy.InputField()
    step_annotation: str = dspy.OutputField(
        desc=f"""Must be one of the following values: {
            [item.value for item in StepAnnotation]}"""
    )
    step_rating: int = dspy.OutputField(
        desc="""A rating between 0 to 5, expressing to what extent to which the given step is both essential and logically valid.

Example: 
    0 denotes 'certainly unnecessary and illogical'
    1 denotes 'certainly unnecessary and most probably invalid'
    2 denotes 'unnecessary and probably invalid'
    3 denotes 'might be necessary and could be valid'
    4 denotes 'seems somewhat necessary but appears valid'
    5 denotes 'certainly necessary and logically sound'
"""
    )


class MessageWithUnderstanding(dspy.BaseModel):
    clear_rephrasing_of_message: str = dspy.Field(
        description="Rephrase the user's message in clearer form. Leave it empty unless a rephrasing is useful."
    )
    why_is_user_asking_this: str = dspy.Field(
        description="Why is the user asking this message at this point in the ongoing chat?"
    )
    what_is_user_objective: str = dspy.Field(
        description="What are user's overall objectives implicit or explicit within this chat?"
    )
    message_decomposition: list[str] = dspy.Field(
        description="Break down the message into simpler sub-messages."
    )


class UnderstandMessage(dspy.Signature):
    chat: list[str] = dspy.InputField(desc="The conversational history till now")
    new_message: str = dspy.InputField(desc="A new message by the user")
    structured_message: MessageWithUnderstanding = dspy.OutputField(
        desc="Message understood in terms of the underlying intent and objective"
    )


class ConversationalResponse(dspy.Signature):
    raw_message_from_user: str = dspy.InputField()
    structured_message: str = dspy.InputField()
    rationale: str = dspy.InputField(
        desc="Rationale behind the conversational response"
    )
    response_to_user: str = dspy.OutputField(
        desc="Response to the user in a conversational style"
    )


class Task(dspy.Module):
    def __init__(self):
        self.generate = dspy.ChainOfThought("message -> rationale, answer")

    def forward(self, structured_message: MessageWithUnderstanding) -> dspy.Prediction:
        answer = self.generate(message=str(structured_message))
        return answer


def rationale_to_steps(rationale: str, max_spaces: int = 2) -> list[str]:
    pattern = r"(?<!\w\.\w.)(?<![A-Z][a-z]\.)(?<=\.|\?)\s"
    sentences = re.split(pattern, rationale)
    filtered_list = [
        sentence for sentence in sentences if sentence.count(" ") >= max_spaces
    ]
    return filtered_list


class VerificationStrategy(str, Enum):
    LLM_AS_A_JUDGE = "llm_as_a_judge"
    RM_MODEL = "rm_model"
    BERT_CLASSIFIER = "bert_classifier"


class StepVerifierType(Protocol):
    @property
    def type(self) -> VerificationStrategy: ...

    def verify_step(
        self,
        objective: str,
        step_to_be_verified: str,
        reasoning_chain: list[str],
        chat_history: list[str] = [],
    ) -> Tuple[StepAnnotation, int]:
        """Verify this step, using this particular step verification method"""
        ...


class JudgeLmVerifier:
    def __init__(self, model: str):
        self.lm = dspy.OllamaLocal(model=model)
        self.llm_judge = TypedChainOfThought(StepVerfication)

    @property
    def type(self) -> VerificationStrategy:
        return VerificationStrategy.LLM_AS_A_JUDGE

    def verify_step(
        self,
        objective: str,
        step_to_be_verified: str,
        reasoning_chain: list[str],
        chat_history: list[str] = [],
    ) -> Tuple[StepAnnotation, int]:
        """Verify this step, using an LLM as a Judge"""
        with dspy.context(lm=self.lm):
            judgement = self.llm_judge(
                objective=objective,
                step_to_be_verified=step_to_be_verified,
                reasoning_chain="\n  - ".join(reasoning_chain),
                chat_history=chat_history,
            )

            annotation, score = judgement.step_annotation, (judgement.step_rating * 0.2)

            print(annotation, score)

            return annotation, score


class BertClassifierVerifier:
    def __init__(
        self,
        tokenizer: AutoTokenizer,
        model: AutoModel,
        threshold: float = 0.7,
        debug: bool = True,
    ):
        self.debug = debug
        self.threshold = threshold
        self.model = model
        self.tokenizer = tokenizer

    @property
    def type(self) -> VerificationStrategy:
        return VerificationStrategy.BERT_CLASSIFIER

    def verify_step(
        self,
        objective: str,
        step_to_be_verified: str,
        reasoning_chain: list[str],
        chat_history: list[str] = [],
    ) -> Tuple[StepAnnotation, int]:
        """Verify this step, using this BERT based classification models such as Cappy"""
        instruction = f"""Does the following answer meet the objective behind user's messages?

        Objectives: {objective}
        """
        instruction += "\n".join(chat_history + [f"Answer: {step_to_be_verified}"])
        response = step_to_be_verified

        # print(instruction) if self.debug else ...
        # print(response) if self.debug else ...

        inputs = self.tokenizer(
            [
                (instruction, response),
            ],
            return_tensors="pt",
        ).to(DEVICE)
        score = self.model(**inputs).logits[0][0].item()

        print(score)
        return (
            (
                StepAnnotation.DOES_NOT_SEEM_RIGHT
                if score <= self.threshold
                else StepAnnotation.ESSENTIAL_AND_VALID
            ),
            score,
        )


# class RmVerifier:
#     def __init__(self, model: str):
#         pass
#
#     @property
#     def type(self) -> VerificationStrategy: ...
#
#     def verify_step(
#         self,
#         chat_history: list[str],
#         objective: str,
#         step_to_be_verified: str,
#     ) -> StepAnnotation:
#         """Verify this step, using this particular step verification method"""
#         ...


class VerifiedQA(dspy.Module):
    def __init__(
        self,
        step_verifier: StepVerifierType,
        objective_verifier: Optional[StepVerifierType] = None,
    ):
        super().__init__()
        self.message_understanding = TypedChainOfThought(UnderstandMessage)
        self.task = Task()
        self.conversational = TypedChainOfThought(ConversationalResponse)

        self.step_verifier = step_verifier
        self.objective_verifier = (
            step_verifier if objective_verifier == None else objective_verifier
        )

    def forward(
        self, message: str, chat_history: list[str] = []
    ) -> Tuple[list[Tuple[str, int]], dspy.Prediction]:
        structured_message: MessageWithUnderstanding = self.message_understanding(
            chat=chat_history, new_message=message
        ).structured_message
        # print(structured_message)

        # TODO: Format Chat and Chat history
        # chat_history.append(message)

        answer = self.task(structured_message)
        steps = rationale_to_steps(answer.rationale)

        dspy.Assert(
            result=(len(steps) > 2),
            msg="There should atleast be 2 steps in our rationale, or its probably not a good rationale.",
        )

        print(f"{len(steps)=}")

        def process_step(step):
            annotation, score = self.step_verifier.verify_step(
                objective=structured_message.what_is_user_objective,
                chat_history=chat_history + [message],
                reasoning_chain=steps,
                step_to_be_verified=step,
            )
            dspy.Suggest(
                result=annotation == StepAnnotation.ESSENTIAL_AND_VALID.value,
                msg="Each step in the thought process must be necessary for reaching an answer and be logically and factually valid.",
            )
            print("Suggest Passed!")
            return step, score

        try:
            with ThreadPoolExecutor(max_workers=20) as executor:
                chosen_steps = list(executor.map(process_step, steps))
        except dspy.DSPySuggestionError as e:
            raise e from e

        response = self.conversational(
            raw_message_from_user=message,
            structured_message=str(structured_message),
            rationale=answer.answer,
        )

        objective_annotation = self.objective_verifier.verify_step(
            objective=structured_message.what_is_user_objective,
            step_to_be_verified=response.response_to_user,
            chat_history=chat_history,
            reasoning_chain=steps,
        )

        dspy.Suggest(
            result=(objective_annotation == StepAnnotation.ESSENTIAL_AND_VALID),
            msg="The answer must meet the user's objectives.",
        )

        return chosen_steps, response


@app.command()
def chat(
    message: str,
    debug: Annotated[
        bool, typer.Option(help="If debug, values should be printed to stdout.")
    ] = False,
    model: Annotated[
        LanguageModel, typer.Option(help="Name of one of the local models.")
    ] = LanguageModel.M_7_B,
):
    lm = dspy.OllamaLocal(model=model.value)

    cappy_tokenizer = AutoTokenizer.from_pretrained("btan2/cappy-large")
    cappy = AutoModelForSequenceClassification.from_pretrained("btan2/cappy-large").to(
        DEVICE
    )
    cappy_verifier = BertClassifierVerifier(tokenizer=cappy_tokenizer, model=cappy)

    # roscoe_tokenizer = AutoTokenizer.from_pretrained("facebook/roscoe-512-roberta-base")
    # roscoe = AutoModelForSequenceClassification.from_pretrained(
    #     "facebook/roscoe-512-roberta-base"
    # ).to(DEVICE)
    # roscoe_verifier = BertClassifierVerifier(tokenizer=roscoe_tokenizer, model=roscoe)

    with dspy.context(lm=lm, trace=[]):
        agent = assert_transform_module(
            VerifiedQA(
                step_verifier=cappy_verifier, objective_verifier=cappy_verifier
            ).map_named_predictors(Retry),
            functools.partial(backtrack_handler, max_backtracks=2),
        )

        reasoning, response = agent(message)
        print(response.response_to_user)
        print(reasoning)

    if debug:
        print(lm.inspect_history(n=5))


@app.command()
def cappy(
    step: str,
    debug: Annotated[
        bool, typer.Option(help="If debug, values should be printed to stdout.")
    ] = False,
):
    cappy_tokenizer = AutoTokenizer.from_pretrained("btan2/cappy-large")
    cappy_model = AutoModelForSequenceClassification.from_pretrained(
        "btan2/cappy-large"
    ).to(DEVICE)

    cappy = BertClassifierVerifier(tokenizer=cappy_tokenizer, model=cappy_model)
    objective = "build a small rocket that can reach the moon"

    score = cappy.verify_step(
        objective=objective,
        step_to_be_verified=step,
        chat_history=[],
        reasoning_chain=[],
    )
    print(score)


if __name__ == "__main__":
    app()
