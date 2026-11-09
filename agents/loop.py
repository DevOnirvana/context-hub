"""The agent loop (SPEC 10): a plain Claude tool-use loop with a step cap of 6.

The model decides the next step; the hub decides what is remembered and who sees it. Per
customer turn:
1. code fetches the policy-filtered context packet once (one set of access-log rows);
2. the customer's reply-style preference, if any, goes into the reply instructions;
3. the model may call its role's tools, up to 6 model calls; code runs every tool;
4. a refusal, or hitting the step cap, hands the conversation to a human with the packet
   (SPEC 17.35), and the agent stops for this conversation.

The history is append-only and keeps every content block exactly as the model returned it:
Sonnet 5.5 checks that earlier turns were not edited, and identical requests are what make
replay hit the cache.
"""
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from agents.llm import LLM, LLMRefused, content_to_params
from agents.tools import CODE_ESCALATIONS, ToolContext, escalate, run_tool, tools_for
from hub.clock import to_ist
from hub.interface import ContextHub, Session
from hub.packet import ContextPacket

STEP_CAP = 6          # model calls per customer turn: bounded agency, bounded cost
MAX_TOKENS = 4000     # a short chat reply plus tool calls; hitting it raises, never truncates silently
EFFORT = "low"        # chat-style servicing; adaptive thinking stays on (Sonnet 5.5 cannot disable it)
PROMPTS = Path(__file__).resolve().parent / "prompts"
HANDOVER = ("I'm passing this to a colleague who can see everything we've discussed. "
            "They'll pick it up from here.")


def system_prompt(role: str) -> str:
    """Base rules plus the role. Plain files, cached as the stable prefix."""
    return (PROMPTS / "base.md").read_text() + "\n" + (PROMPTS / f"{role}.md").read_text()


def reply_instructions(packet: ContextPacket) -> str | None:
    """The customer-context preference, injected into the reply instructions (SPEC 10), so the
    Wednesday reply visibly changes. Only allowlisted keys reach the packet (admission test 1)."""
    lines = []
    for pref in packet.customer_context:
        if pref.key == "preferred_explanation_style":
            lines.append(f"This customer asked for explanations in this style: {pref.value}. Use short sentences "
                         "and everyday words. Avoid banking terms (for example 'provisional credit', 'statement "
                         "balance', 'minimum amount due', 'chargeback'); say what they mean in plain words.")
        elif pref.key == "preferred_language":
            lines.append(f"This customer prefers this language: {pref.value}.")
        elif pref.key == "preferred_contact_channel":
            lines.append(f"This customer prefers to be contacted by: {pref.value}.")
    return "\n".join(lines) or None


def turn_message(packet: ContextPacket, customer_text: str) -> str:
    now = to_ist(packet.as_of)
    return (f"<now>{now:%A %d %B %Y, %H:%M} IST</now>\n"
            f"<context_packet>\n{packet.model_dump_json()}\n</context_packet>\n"
            f"<customer_turn>\n{customer_text}\n</customer_turn>")


@dataclass
class ToolCall:
    name: str
    args: dict
    result: str      # JSON text exactly as the model received it
    is_error: bool


@dataclass
class TurnResult:
    reply: str
    packet: ContextPacket           # what the agent saw this turn (the UI's "what the agent sees")
    tool_calls: list[ToolCall]
    steps: int                      # model calls made
    escalated: bool
    writes: list[str]               # what changed in the hub, in order


@dataclass
class Conversation:
    hub: ContextHub
    llm: LLM
    session: Session
    messages: list[dict] = field(default_factory=list)   # append-only
    closed: bool = False                                  # handed to a human by code

    @property
    def role(self) -> str:
        return self.session.agent_role

    def turn(self, customer_text: str, on_event: Callable[[str, object], None] | None = None) -> TurnResult:
        """One customer turn. on_event(kind, detail) reports progress for the UI: ("thinking", step)
        before each model call, ("tool", (name, ok)) after each tool, ("handover", reason).

        If the turn fails (network, budget, a cache miss), the messages it added are removed, so
        a retry sends exactly a prefix the API has already seen: history stays append-only from
        the API's point of view. Hub writes made by tools before the failure stay: they happened."""
        if self.closed:
            raise RuntimeError("this conversation was handed to a human; start a new one")
        start = len(self.messages)
        try:
            return self._turn(customer_text, on_event or (lambda kind, detail: None))
        except Exception:
            del self.messages[start:]
            raise

    def _turn(self, customer_text: str, on_event: Callable[[str, object], None]) -> TurnResult:
        packet = self.hub.get_context(self.session)
        ctx = ToolContext(hub=self.hub, llm=self.llm, session=self.session, packet=packet,
                          customer_turn=customer_text)
        self.messages.append({"role": "user", "content": turn_message(packet, customer_text)})
        system, extra, tools = system_prompt(self.role), reply_instructions(packet), tools_for(self.role)
        calls: list[ToolCall] = []

        for step in range(1, STEP_CAP + 1):
            on_event("thinking", step)
            try:
                result = self.llm.call(purpose=f"agent:{self.role}", model=self.llm.config.model_id, system=system,
                                       system_extra=extra, messages=self.messages, tools=tools,
                                       max_tokens=MAX_TOKENS, effort=EFFORT, cache_messages=True)
            except LLMRefused:
                on_event("handover", "model_refusal")
                return self._hand_over(ctx, packet, calls, step, "model_refusal")
            message = result.message
            self.messages.append({"role": "assistant", "content": content_to_params(message)})
            uses = [b for b in message.content if b.type == "tool_use"]
            if message.stop_reason != "tool_use" or not uses:
                reply = "\n".join(b.text for b in message.content if b.type == "text").strip()
                return TurnResult(reply=reply, packet=packet, tool_calls=calls, steps=step,
                                  escalated=ctx.escalated, writes=ctx.writes)
            # Every tool_use gets its result, all in ONE user message (the API pairs them by id).
            results = []
            for use in uses:
                text, is_error = run_tool(ctx, use.name, dict(use.input))
                calls.append(ToolCall(name=use.name, args=dict(use.input), result=text, is_error=is_error))
                on_event("tool", (use.name, not is_error))
                results.append({"type": "tool_result", "tool_use_id": use.id, "content": text,
                                **({"is_error": True} if is_error else {})})
            self.messages.append({"role": "user", "content": results})

        on_event("handover", "step_cap")
        return self._hand_over(ctx, packet, calls, STEP_CAP, "step_cap")

    def _hand_over(self, ctx: ToolContext, packet: ContextPacket, calls: list[ToolCall], steps: int,
                   why: str) -> TurnResult:
        """Code escalates (SPEC 17.35): a human gets the packet; the agent stops for this conversation."""
        if ctx.packet.cases:
            escalate(ctx, ctx.packet.cases[0].case_id, CODE_ESCALATIONS[why])
        self.closed = True
        return TurnResult(reply=HANDOVER, packet=packet, tool_calls=calls, steps=steps, escalated=True,
                          writes=ctx.writes)
