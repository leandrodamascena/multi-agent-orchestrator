import json
from typing import AsyncIterable, Optional, Any, AsyncGenerator
from typing import Any, AsyncIterable, Optional
from dataclasses import dataclass, field
import re
from anthropic import AsyncAnthropic, Anthropic
from multi_agent_orchestrator.agents import Agent, AgentOptions, AgentStreamResponse
from multi_agent_orchestrator.types import (ConversationMessage,
                       ParticipantRole,
                       TemplateVariables,
                       AgentProviderType)
from multi_agent_orchestrator.utils import Logger, AgentTools, AgentTool
from multi_agent_orchestrator.retrievers import Retriever

@dataclass
class AnthropicAgentOptions(AgentOptions):
    api_key: Optional[str] = None
    client: Optional[Any] = None
    model_id: str = "claude-3-5-sonnet-20240620"
    streaming: Optional[bool] = False
    inference_config: Optional[dict[str, Any]] = None
    retriever: Optional[Retriever] = None
    tool_config: Optional[dict[str, Any] | AgentTools] = None
    custom_system_prompt: Optional[dict[str, Any]] = None



class AnthropicAgent(Agent):
    def __init__(self, options: AnthropicAgentOptions):
        super().__init__(options)

        if not options.api_key and not options.client:
            raise ValueError("Anthropic API key or Anthropic client is required")

        self.streaming = options.streaming

        if options.client:
            if self.streaming:
                if not isinstance(options.client, AsyncAnthropic):
                    raise ValueError("If streaming is enabled, the provided client must be an AsyncAnthropic client")
            elif not isinstance(options.client, Anthropic):
                raise ValueError("If streaming is disabled, the provided client must be an Anthropic client")
            self.client = options.client
        elif self.streaming:
            self.client = AsyncAnthropic(api_key=options.api_key)
        else:
            self.client = Anthropic(api_key=options.api_key)

        self.system_prompt = ''
        self.custom_variables = {}

        self.default_max_recursions: int = 5

        self.model_id = options.model_id

        default_inference_config = {
            'maxTokens': 1000,
            'temperature': 0.1,
            'topP': 0.9,
            'stopSequences': []
        }

        if options.inference_config:
            self.inference_config = {**default_inference_config, **options.inference_config}
        else:
            self.inference_config = default_inference_config

        self.retriever = options.retriever
        self.tool_config: Optional[dict[str, Any]] = options.tool_config

        self.prompt_template: str = f"""You are a {self.name}.
        {self.description}
        Provide helpful and accurate information based on your expertise.
        You will engage in an open-ended conversation,
        providing helpful and accurate information based on your expertise.
        The conversation will proceed as follows:
        - The human may ask an initial question or provide a prompt on any topic.
        - You will provide a relevant and informative response.
        - The human may then follow up with additional questions or prompts related to your previous
        response, allowing for a multi-turn dialogue on that topic.
        - Or, the human may switch to a completely new and unrelated topic at any point.
        - You will seamlessly shift your focus to the new topic, providing thoughtful and
        coherent responses based on your broad knowledge base.
        Throughout the conversation, you should aim to:
        - Understand the context and intent behind each new question or prompt.
        - Provide substantive and well-reasoned responses that directly address the query.
        - Draw insights and connections from your extensive knowledge when appropriate.
        - Ask for clarification if any part of the question or prompt is ambiguous.
        - Maintain a consistent, respectful, and engaging tone tailored
        to the human's communication style.
        - Seamlessly transition between topics as the human introduces new subjects."""

        if options.custom_system_prompt:
            self.set_system_prompt(
                options.custom_system_prompt.get('template'),
                options.custom_system_prompt.get('variables')
            )

    def is_streaming_enabled(self) -> bool:
        return self.streaming is True

    async def _prepare_system_prompt(self, input_text: str) -> str:
        """Prepare the system prompt with optional retrieval context."""

        self.update_system_prompt()
        system_prompt = self.system_prompt

        if self.retriever:
            response = await self.retriever.retrieve_and_combine_results(input_text)
            system_prompt += f"\nHere is the context to use to answer the user's question:\n{response}"

        return system_prompt

    def _prepare_conversation(
        self,
        input_text: str,
        chat_history: list[ConversationMessage]
    ) -> list[Any]:
        """Prepare the conversation history with the new user message."""

        messages = [{"role": "user" if msg.role == ParticipantRole.USER.value else "assistant",
                     "content": msg.content[0]['text'] if msg.content else ''} for msg in chat_history]
        messages.append({"role": "user", "content": input_text})

        return messages

    def _prepare_tool_config(self) -> dict:
        """Prepare tool configuration based on the tool type."""

        if isinstance(self.tool_config["tool"], AgentTools):
            return self.tool_config["tool"].to_claude_format()

        if isinstance(self.tool_config["tool"], list):
            return [
                    tool.to_claude_format() if isinstance(tool, AgentTool) else tool
                    for tool in self.tool_config['tool']
                ]

        raise RuntimeError("Invalid tool config")

    def _build_input(
            self,
            messages: list[Any],
            system_prompt: str
            ) -> dict:
        """Build the conversation command with all necessary configurations."""
        json_input = {
            "model": self.model_id,
            "max_tokens": self.inference_config.get('maxTokens'),
            "messages": messages,
            "system": system_prompt,
            "temperature": self.inference_config.get('temperature'),
            "top_p": self.inference_config.get('topP'),
            "stop_sequences": self.inference_config.get('stopSequences'),
        }

        if self.tool_config:
            json_input["tools"] = self._prepare_tool_config()

        return json_input

    def _get_max_recursions(self) -> int:
        """Get the maximum number of recursions based on tool configuration."""
        if not self.tool_config:
            return 1
        return self.tool_config.get('toolMaxRecursions', self.default_max_recursions)

    async def _handle_streaming(
        self,
        payload_input: dict,
        messages: list[Any],
        max_recursions: int
    ) -> AsyncIterable[Any]:
        """Handle streaming response processing with tool recursion."""
        continue_with_tools = True
        final_response = None

        async def stream_generator():
            nonlocal continue_with_tools, final_response, max_recursions

            while continue_with_tools and max_recursions > 0:
                response = self.handle_streaming_response(payload_input)

                async for chunk in response:
                    if chunk.final_message:
                        final_response = chunk.final_message # do not yield the full message as it need to be converted in Conversation Message
                    else:
                        yield chunk

                if any('tool_use' in content.type for content in final_response.content):
                    payload_input['messages'].append({"role": "assistant", "content": final_response.content})
                    tool_response = await self._process_tool_block(final_response, messages)
                    payload_input['messages'].append(tool_response)
                else:
                    continue_with_tools = False
                    # yield las message
                    yield AgentStreamResponse(final_message=ConversationMessage(role=ParticipantRole.ASSISTANT.value, content=[{"text": final_response.content[0].text}]))

                max_recursions -= 1

        return stream_generator()

    async def _process_with_strategy(
        self,
        streaming: bool,
        payload_input: dict,
        messages: list[Any]
    ) -> ConversationMessage | AsyncIterable[Any]:
        """Process the request using the specified strategy."""

        max_recursions = self._get_max_recursions()

        if streaming:
            return await self._handle_streaming(payload_input, messages, max_recursions)
        return await self._handle_single_response_loop(payload_input, messages, max_recursions)

    async def _process_tool_block(self, llm_response: Any, conversation: list[Any]) -> Any:
        if 'useToolHandler' in  self.tool_config:
            # tool process logic is handled elsewhere
            tool_response = await self.tool_config['useToolHandler'](llm_response, conversation)
        elif isinstance(self.tool_config['tool'], AgentTools):
            tool_response = await self.tool_config['tool'].tool_handler(AgentProviderType.ANTHROPIC.value, llm_response, conversation)
        else:
            raise ValueError("You must use class when not providing a custom tool handler")
        return tool_response

    async def _handle_single_response_loop(
        self,
        payload_input: Any,
        messages: list[Any],
        max_recursions: int
    ) -> ConversationMessage:
        """Handle single response processing with tool recursion."""

        continue_with_tools = True
        llm_response = None

        while continue_with_tools and max_recursions > 0:
            llm_response = await self.handle_single_response(payload_input)
            if any('tool_use' in content.type for content in llm_response.content):
                payload_input['messages'].append({"role": "assistant", "content": llm_response.content})
                tool_response = await self._process_tool_block(llm_response, messages)
                payload_input['messages'].append(tool_response)
            else:
                continue_with_tools = False

            max_recursions -= 1

        return ConversationMessage(role=ParticipantRole.ASSISTANT.value, content=[{"text": llm_response.content[0].text}])

    async def process_request(
        self,
        input_text: str,
        user_id: str,
        session_id: str,
        chat_history: list[ConversationMessage],
        additional_params: Optional[dict[str, str]] = None
    ) -> ConversationMessage | AsyncIterable[Any]:

        messages = self._prepare_conversation(input_text, chat_history)
        system_prompt = await self._prepare_system_prompt(input_text)
        json_input = self._build_input(messages, system_prompt)

        return await self._process_with_strategy(self.streaming, json_input, messages)

    async def handle_single_response(self, input_data: dict) -> Any:
        try:
            return self.client.messages.create(**input_data)
        except Exception as error:
            Logger.error(f"Error invoking Anthropic: {error}")
            raise error

    async def handle_streaming_response(self, input) -> AsyncGenerator[AgentStreamResponse, None]: # noqa: A002
        accumulated: dict[str, Any] = {}

        try:
            async with self.client.messages.stream(**input) as stream:
                async for event in stream:
                    if event.type == "text":
                        self.callbacks.on_llm_new_token(event.text)
                        yield AgentStreamResponse(text=event.text)
                    elif event.type == "content_block_stop":
                        recursions = 0
                        break

                # you can still get the accumulated final message outside of
                # the context manager, as long as the entire stream was consumed
                # inside of the context manager

                accumulated = await stream.get_final_message()
            # we need to yield the whole content to keep the tool use block
            yield AgentStreamResponse(
                final_message=ConversationMessage(role=ParticipantRole.ASSISTANT.value,
                                                  content=accumulated.content))

        except Exception as error:
            Logger.error(f"Error getting stream from Anthropic model: {str(error)}")
            raise error


    def set_system_prompt(self,
                          template: Optional[str] = None,
                          variables: Optional[TemplateVariables] = None) -> None:
        if template:
            self.prompt_template = template
        if variables:
            self.custom_variables = variables
        self.update_system_prompt()

    def update_system_prompt(self) -> None:
        all_variables: TemplateVariables = {**self.custom_variables}
        self.system_prompt = self.replace_placeholders(self.prompt_template, all_variables)

    @staticmethod
    def replace_placeholders(template: str, variables: TemplateVariables) -> str:
        def replace(match):
            key = match.group(1)
            if key in variables:
                value = variables[key]
                return '\n'.join(value) if isinstance(value, list) else str(value)
            return match.group(0)

        return re.sub(r'{{(\w+)}}', replace, template)
