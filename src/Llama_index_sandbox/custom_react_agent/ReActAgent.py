import copy
import json
import logging
from typing import Optional, List, Tuple, cast

from llama_index.agent import ReActAgent
from llama_index.agent.react.types import BaseReasoningStep, ActionReasoningStep, ObservationReasoningStep
from llama_index.callbacks import trace_method, CBEventType, EventPayload
from llama_index.chat_engine.types import AgentChatResponse
from llama_index.llms import ChatMessage, MessageRole, ChatResponse
from llama_index.utils import print_text

from src.Llama_index_sandbox.prompts import QUERY_ENGINE_PROMPT_FORMATTER, QUERY_ENGINE_TOOL_DESCRIPTION, QUERY_ENGINE_TOOL_ROUTER


class CustomReActAgent(ReActAgent):
    from typing import List

    @trace_method("chat")
    def chat(
            self, message: str, chat_history: Optional[List[ChatMessage]] = None
    ) -> AgentChatResponse:
        """Chat."""
        if chat_history is not None:
            self._memory.set(chat_history)

        # TODO 2023-10-17: it feels to be like running in circles in somewhat biasing the agent to not rely on its prior knowledge and have it use the query engine.
        #  Perhaps this will go away once the LLM is trained on local data.
        message_with_tool_description = f"{message}\n{QUERY_ENGINE_TOOL_ROUTER}"
        self._memory.put(ChatMessage(content=message_with_tool_description, role="user"))

        current_reasoning: List[BaseReasoningStep] = []

        # start loop
        for _ in range(self._max_iterations):
            # prepare inputs
            input_chat = self._react_chat_formatter.format(
                chat_history=self._memory.get(), current_reasoning=current_reasoning
            )
            # NOTE 2023-10-15: the observation from the query tool is passed to the LLM which then answers with Thought or Answer,
            # hence the parser does not have an Observation case
            # send prompt
            chat_response = self._llm.chat(input_chat)

            # Create a deep copy of chat_response for modification
            chat_response_copy = copy.deepcopy(chat_response)

            # Enforce user question into Action Input
            response_content = chat_response_copy.raw['choices'][0]['message']['content']
            # NOTE 2023-10-15: we force the input to the query engine to be the user question.
            #  Otherwise, GPT greatly simplifies the question, and the query engine does very poorly.
            if 'Action Input:' in response_content:
                # Extract the part after 'Action Input:'
                # TODO NOTE 2023-10-15: lets engineer and scrutinise further this part. Beyond passing the question as-is, we can wrap it further e.g.
                #  add "always make a thorough answer", "directly quote the sources of your knowledge in the same sentence in parentheses".
                action_input_part = response_content.split('Action Input:')[1].strip()

                # Modify its "input" value to be the user question
                try:
                    action_input_json = json.loads(action_input_part)
                    augmented_message = QUERY_ENGINE_PROMPT_FORMATTER.format(question=message)
                    action_input_json['input'] = augmented_message

                    # Replace the old part with the modified one
                    response_content = response_content.replace(action_input_part, json.dumps(action_input_json))

                    # Update the deep-copied chat_response accordingly
                    chat_response_copy.raw['choices'][0]['message']['content'] = response_content
                    chat_response_copy.message.content = response_content  # Update this too
                except Exception as e:
                    logging.error(f'Error in modifying the Action Input part of the response_content: [{e}]')

            # given react prompt outputs, call tools or return response
            reasoning_steps, is_done = self._process_actions(output=chat_response_copy)
            current_reasoning.extend(reasoning_steps)

            if is_done:
                break

        response = self._get_response(current_reasoning)
        self._memory.put(
            ChatMessage(content=response.response, role=MessageRole.ASSISTANT)
        )
        return response

    def _process_actions(
            self, output: ChatResponse
    ) -> Tuple[List[BaseReasoningStep], bool]:
        _, current_reasoning, is_done = self._extract_reasoning_step(output)

        if is_done:
            return current_reasoning, True

        # call tool with input
        reasoning_step = cast(ActionReasoningStep, current_reasoning[-1])
        tool = self._tools_dict[reasoning_step.action]
        with self.callback_manager.event(
                CBEventType.FUNCTION_CALL,
                payload={
                    EventPayload.FUNCTION_CALL: reasoning_step.action_input,
                    EventPayload.TOOL: tool.metadata,
                },
        ) as event:
            tool_output = tool.call(**reasoning_step.action_input)
            event.on_end(payload={EventPayload.FUNCTION_OUTPUT: str(tool_output)})

        observation_step = ObservationReasoningStep(observation=str(tool_output))
        current_reasoning.append(observation_step)
        if self._verbose:
            print_text(f"{observation_step.get_content()}\n", color="blue")
        return current_reasoning, False