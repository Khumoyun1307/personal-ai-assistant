# Telegram AI Agent — Complete Project Context & Development Handoff

## 1. Project Overview

I want to design, develop, test, and eventually launch a **Telegram-native Personal AI Agent/Assistant**.

This is not a generic AI chatbot connected to Telegram. It is an intelligent assistant that operates throughout the user's Telegram experience, understands relevant activities and conversations, remembers context, monitors information, executes authorized actions, manages communication, and performs automated tasks.

The agent should feel integrated into Telegram rather than like an external service that happens to have a Telegram plugin.

We have already discussed the product concept, researched Telegram's capabilities and limitations, explored potential functionality, considered existing competing solutions, and outlined an initial technical architecture and implementation strategy.

**Your job is to continue this project from where we stopped, preserving all existing decisions and understanding. Do not restart discovery from zero or reduce the product to a simple chatbot or autoresponder.**

Our next steps are:

1. Finalize and document the Product Requirements.
2. Complete the technical architecture and implementation design.
3. Create a detailed milestone and vertical-slice implementation plan.
4. Establish the development environment and project structure.
5. Begin implementing and testing the product incrementally.
6. Prepare for personal testing, future multi-user support, and potential public commercialization.

Use the context below as the starting point.

---

# 2. Product Vision

Build an intelligent, autonomous-but-user-controlled assistant operating natively within Telegram.

The agent should help users manage their Telegram communications, information, activities, and workflows.

It should understand instructions in natural language, maintain context across interactions, use relevant tools, monitor events, reason about situations, and execute permitted tasks without requiring the user to manually control every step.

It must distinguish between actions it can perform autonomously, actions requiring user approval, and situations where it should notify the user instead of taking action.

### What the agent is

A personal AI agent that can:

- Understand and process Telegram conversations.
- Manage selected private-message interactions on the user's behalf.
- Generate context-aware responses and continue conversations when authorized.
- Monitor messages and relevant events.
- Summarize communications, channels, and accessible information.
- Recognize important information, deadlines, commitments, and unanswered questions.
- Execute scheduled or conditional automations.
- Remember user instructions, preferences, contacts, and relevant context.
- Proactively alert users about important developments.
- Coordinate multiple actions to complete tasks.
- Serve as a personal Telegram assistant through a native bot interface.

### What the agent is not

- Not merely a AI clone inside Telegram.
- Not just an AI answering incoming messages.
- Not exclusively an away-message autoresponder.
- Not primarily a customer-support or business-sales chatbot.
- Not an external productivity tool with Telegram added as a secondary integration.
- Not a collection of disconnected automation scripts.
- Not necessarily a multi-agent system; one capable agent with appropriate tools may be sufficient.

The defining value is an intelligent agent that can understand what is happening within authorized parts of Telegram and help manage it.

---

# 3. Intended Users and Product Evolution

## Initial user

I will be the first user.

We will develop and test the agent with my personal Telegram account.

This provides an opportunity to experiment, validate behaviors, discover problems, and improve the user experience before making it publicly available.

## Potential public product

If personal testing demonstrates usefulness and satisfactory reliability, I intend to make the assistant available to individual Telegram users.

Possible future commercialization includes:

- Free access.
- Freemium access with feature or usage limits.
- Paid subscriptions.
- Premium agent functionality.
- Other viable monetization models.

**We have not selected a business model.**

The architecture should support eventual multi-user operation, individual account connections, isolated memories and permissions, resource limits, and future billing without requiring a full rewrite.

However, we must not overengineer public commercialization before validating the personal product.

## Initial scope

Focus on individual users and private Telegram interactions.

Group-level functionality is explicitly deferred. We do not have a compelling use case for it yet.

Channel-related intelligence is interesting and should remain in the product vision, but its specific implementation depends on Telegram API access and permission restrictions.

Third-party productivity integrations can be explored later, but the central product must remain Telegram-focused.

---

# 4. Agreed User Experience and Telegram Integration

An important discussion concerned whether the assistant should be directly integrated into the user's Telegram account or exist as a separate bot.

We concluded that these are complementary parts of one product, not two competing approaches.

## A. Connected Telegram account

The assistant can receive and process permitted messages from the user's existing Telegram account through Telegram's official connected-business-bot functionality.

Depending on user permissions and API limitations, it can understand incoming conversations, provide assistance, and respond on the user's behalf.

Examples:

- Someone sends the user a question while the user is unavailable.
- The assistant recognizes the context and follows the user's instructions.
- It can respond intelligently rather than sending a fixed away message.
- If the other person replies, the agent understands the continuation.
- It can ask for missing information, answer permitted questions, and recognize situations requiring the user's involvement.
- It can subsequently summarize what happened and notify the user about pending decisions.

This is only one example of agent functionality, not the entire product.

## B. Private assistant chat inside Telegram

The same agent should be accessible through a private Telegram bot conversation.

This is its native user-facing command and interaction interface.

The user can:

- Give natural-language instructions.
- Ask questions about accessible Telegram activity.
- Review summaries.
- Set up automations and reminders.
- Configure preferences.
- Approve or reject suggested actions.
- Receive notifications.
- Review the agent's activity.
- Pause or resume automation.
- Manage settings and permissions.

Illustrative commands:

"Summarize what happened in my messages while I was studying."

"Tell me which conversations need my attention."

"For the next four hours, help with messages from selected contacts."

"If someone asks about the project, answer questions you know and ask for any missing details."

"Notify me when Daniel replies."

"Summarize important updates from the channels I'm monitoring."

"Find the conversation where we agreed on the meeting time."

"Draft a response to Alex, but don't send it until I approve."

"Show me everything the assistant answered on my behalf today."

"Remind me tomorrow if the documents haven't arrived."

The agent should interpret instructions, determine whether an action is feasible and permitted, execute appropriate steps, and report the result.

These instructions are examples of the desired experience, not proof that every action is already supported by Telegram APIs.

## C. One unified agent

The initial architectural recommendation was to use one Telegram bot for both:

1. The assistant's private control interface.
2. The connected account's authorized business-message integration.

Both communicate with the same backend agent.

We do not need two separate AI products or two disconnected intelligence systems.

Conceptual architecture:

Telegram user account / private assistant bot
↓
Telegram integration layer
↓
Shared AI agent backend
↓
Reasoning, memory, tools, permissions, automation
↓
Database, scheduling, authorized actions, notifications

The AI application runs in our backend, but Telegram is the native product experience.

A Telegram Mini App is optional for future settings, dashboards, or complex workflows. We have not decided to build one initially.

---

# 5. Product Capability Areas

The product should be organized around several related capabilities, without artificially forcing them into separate agents.

## 5.1 AI Assistant and Natural-Language Control

The assistant should understand normal conversational requests and translate them into appropriate actions.

Potential functionality includes:

- Asking questions and receiving contextual answers.
- Understanding follow-up instructions.
- Interpreting user preferences.
- Retrieving relevant context.
- Managing and modifying tasks.
- Executing multi-step workflows.
- Explaining what happened.
- Clarifying ambiguous instructions when genuinely necessary.
- Using available tools within permission boundaries.

Its utility should come from real actions and Telegram awareness, not just generating conversational responses.

## 5.2 Intelligent DM Management

The assistant may:

- Monitor permitted incoming private messages.
- Understand conversation context.
- Identify important or urgent messages.
- Detect unanswered questions.
- Detect messages requiring decisions.
- Identify follow-up commitments.
- Categorize or prioritize conversations.
- Prepare replies.
- Answer on the user's behalf when authorized.
- Continue a conversation contextually.
- Escalate sensitive or uncertain situations.
- Recognize when the user resumes the conversation.
- Produce conversation summaries and activity reports.

The system must not assume access to messages outside its actual permissions.

## 5.3 Contextual Automated Communication

The assistant should eventually support personalized conversation handling rather than simple autoresponses.

Illustrative scenario:

The user says:

"I'm studying until 7 PM. Help with messages about our project. Ask people for missing details and interrupt me only if something genuinely requires my decision before 7."

The system should:

- Interpret the instruction.
- Determine which conversations it applies to.
- Observe permitted incoming messages.
- Respond only where authorized.
- Use appropriate context.
- Continue relevant conversations.
- Recognize questions it cannot answer reliably.
- Escalate important situations.
- Avoid unnecessary interruptions.
- Preserve a record of actions.
- Provide a useful catch-up summary afterward.

Users should be able to define different behaviors for different contacts or situations.

Example communication modes:

- Observe only.
- Summarize or notify.
- Suggest replies.
- Draft replies requiring approval.
- Automatically reply within explicitly authorized boundaries.
- Temporarily pause assistant responses.
- Escalate to the account owner.

These are candidate product requirements to define carefully, not a finalized interface specification.

## 5.4 Summaries and Information Management

The assistant should help users understand Telegram information efficiently.

Potential capabilities:

- Summarize recent accessible DMs.
- Summarize what happened during a specific period.
- Identify important conversations.
- Produce catch-up reports.
- Explain outstanding commitments.
- Detect messages that require action.
- Answer questions about retained conversation context.
- Produce periodic communication summaries.
- Explain what the AI itself did during an absence.

The aim is useful information extraction, not simply shortening message text.

## 5.5 Alerts and Monitoring

Users should eventually define monitoring instructions using natural language.

Examples:

"Tell me if someone mentions an urgent deadline."

"Notify me when Alex replies."

"Alert me if anyone asks for a decision about the project."

"Find important news in the channels I've authorized the assistant to monitor."

The agent should differentiate routine informational messages from genuinely actionable or urgent notifications.

Notifications should respect user preferences and avoid unnecessary noise.

## 5.6 Scheduling and Conditional Automation

Potential capabilities include:

- Scheduled reminders.
- Time-based agent instructions.
- Temporary availability modes.
- Conditional alerts.
- Follow-up tracking.
- Pending-task management.
- Scheduled summaries.
- Scheduled actions subject to Telegram permissions.
- Multi-step workflows.

Examples:

"Remind me to follow up tomorrow."

"When Sarah replies, summarize the answer."

"At the end of the day, show me conversations needing attention."

"If the requested document hasn't been received by tomorrow, remind me."

We must distinguish between:

- Sending a reminder to the assistant's owner.
- Sending a message on behalf of the user's Telegram account.
- Monitoring a condition.
- Taking action when that condition is met.

These operations have different technical and authorization constraints.

## 5.7 User Personalization and Memory

The assistant should remember relevant information so users don't have to repeat instructions constantly.

Possible memory categories:

**User profile:** Personal instructions, communication preferences, availability rules, preferred behaviors.

**Conversation context:** Relevant accessible messages and current discussions.

**Contact context:** User-authorized contact-specific preferences and contextual facts.

**Task memory:** Pending decisions, commitments, reminders, scheduled actions and incomplete workflows.

**Long-term summaries:** Compact representations of important past interactions within retention permissions.

**Action history:** What the agent proposed, executed, declined or escalated.

Memory should remain controlled by the user, appropriately isolated, and subject to retention and deletion rules.

We should not confuse storing every message forever with having intelligent memory.

## 5.8 Channel Intelligence

I am interested in letting the agent summarize, monitor, and extract relevant information from Telegram channels.

Possible examples:

- Summarize important posts from selected channels.
- Provide topic-specific updates.
- Identify relevant news or opportunities.
- Alert users about selected conditions.
- Answer questions about information accessible to the assistant.

However, this area has significant API access constraints.

We must explicitly investigate the official and permissible access mechanisms.

A standard or business bot does not automatically gain access to every channel a user follows, nor to arbitrary historical messages.

Do not promise or implement unauthorized collection.

If the scope requires a different Telegram integration mechanism, research its tradeoffs, policy implications, security requirements, and suitability before proposing it.

Channel features may require a separate implementation phase.

## 5.9 Potential Future Capabilities

Explore later if justified:

- Voice messages and audio understanding.
- File and document processing.
- Advanced search and retrieval.
- More sophisticated contact-aware communication.
- Additional agent tools.
- Authorized integrations with external services.
- Telegram Mini App settings dashboard.
- Advanced conditional workflows.
- More flexible user automation.
- Group-level functionality if compelling use cases emerge.

Do not let speculative features delay a working initial product.

---

# 6. Telegram API Research Findings

Previous research identified Telegram's official connected-business-bot functionality, also referred to as Secretary Mode, as the preferred initial integration method.

**These findings should be verified against the current official Telegram documentation before implementation. Do not treat this handoff as a replacement for API documentation.**

Reported findings:

1. A Telegram bot can connect to a user's account as a business bot.
2. The user controls relevant business-bot permissions and chat access.
3. The bot can receive permitted business-message events.
4. It can respond on behalf of the connected user where permitted.
5. Business connections can be paused or disconnected.
6. The previous research indicated that users can connect a business bot without Telegram Premium.
7. Telegram restricts account-level reply permissions, including the requirement for an incoming message within the preceding 24 hours.
8. Only one connected business bot can be attached to an account at a time.
9. The connection does not provide unrestricted access to historical conversations, all channels, or arbitrary outbound messages.

**I explicitly accept the 24-hour reply restriction.**

We do not need to work around it simply to send late replies. The assistant should reply within the permitted window and avoid unauthorized actions outside it.

If the business connection cannot perform a requested action, the assistant should explain the limitation or use a clearly permitted alternative.

Important distinction:

A normal Telegram bot can receive private instructions directly from its user.

A business-connected bot can additionally interact with permitted account conversations.

These capabilities must not be confused.

## Official documentation to consult

- https://core.telegram.org/bots/api
- https://core.telegram.org/bots/features
- https://core.telegram.org/api/bots/connected-business-bots
- https://core.telegram.org/constructor/businessBotRights
- https://core.telegram.org/bots/webhooks
- https://core.telegram.org/bots/payments

Verify exact connection requirements, message/event types, account permissions, outgoing message restrictions, bot commands, update handling, and relevant platform policies.

## Alternative integration: Telegram full client API / MTProto

We briefly identified the possibility of integrating through Telegram's full user-client API.

It may offer broader account functionality than the Bot API, but introduces substantially different security, session-management, operational and compliance risks.

It is not the selected initial architecture.

Investigate it only if a compelling requirement cannot be supported through official business-bot functionality, and do not silently substitute it for the approved approach.

---

# 7. Competitive and Existing Implementation Research

Previous exploration found open-source implementations that overlap with portions of our product.

These are references to investigate, not models to copy uncritically.

## SmarTel

https://github.com/AliZakaee/SmarTel

Previously identified as a Telegram Business AI responder with features such as reply approvals, knowledge handling, contact context and takeover controls.

## tg-secretary

https://github.com/sepehr071/tg-secretary

Previously identified as a personal Telegram assistant exploring conversational memory, communication style, relationship-aware replies and human approvals.

## Hermes Telegram Business

https://github.com/NousResearch/hermes-telegram-business

Previously identified as an integration for observing business messages and proposing replies.

Review these projects where useful to understand implementation patterns, limitations, maintenance state and relevant technical choices.

Verify their actual present capabilities before drawing conclusions.

Do not copy their code without checking licenses and suitability.

## Product differentiation

Our intended product is broader than an AI autoresponder.

The goal is a unified Telegram-first agent capable of understanding communications, monitoring information, maintaining context, coordinating tasks, and automating permitted actions.

An example of this broader experience is:

1. Understand a conversation.
2. Recognize a commitment.
3. Create a pending follow-up.
4. Monitor the relevant accessible conversation.
5. Detect an incoming reply.
6. Update the task state.
7. Notify or summarize the result.

This should function as one coherent agent workflow, not several disconnected features.

We have not yet established verified market demand, pricing, or a final competitive advantage. These remain future product-validation questions.

---

# 8. Preliminary Technical Architecture

We discussed starting with a modular, relatively simple Python backend.

The goal is maintainability, reliable automation, security, and eventual multi-user support without premature complexity.

## Previously recommended stack

| Component | Proposed technology |
|---|---|
| Language | Python |
| Telegram bot library | aiogram 3 |
| HTTP application | FastAPI |
| Primary database | PostgreSQL |
| Database access | SQLAlchemy |
| Schema migrations | Alembic |
| AI intelligence | API-based LLM supporting tool calling |
| Scheduling | Database-backed persistent jobs and background worker |
| Deployment | Docker on an always-on host |
| Automated testing | pytest and mocked integrations |

These are **proposed technical choices**, not irreversible requirements.

Validate them against current frameworks, Telegram features, deployment requirements and project needs before finalizing.

We have not yet selected:

- The specific LLM provider or model.
- The final hosting provider.
- The exact deployment topology.
- The database schema.
- The precise scheduler or queue implementation.
- The finalized agent orchestration approach.
- The storage/retention policy.
- The external integrations for future versions.

Avoid unnecessary microservices, complicated multi-agent frameworks, premature vector databases, and other infrastructure without demonstrated need.

A well-designed modular monolith is an appropriate starting point.

## Conceptual architecture

**Telegram Interfaces**

- Private assistant conversation.
- Connected business account messages.
- Future authorized Telegram sources.

↓

**Telegram Integration Layer**

- Incoming events.
- User and account identification.
- Connection state.
- Permissions.
- Event routing.
- Update validation.

↓

**AI Agent Engine**

- Intent interpretation.
- Reasoning and planning.
- Context retrieval.
- Memory.
- Tool selection.
- User instructions.
- Action proposals.

↓

**Authorization and Execution Layer**

- Deterministic permissions.
- Human approval.
- Telegram API actions.
- Automation rules.
- Scheduling.
- Execution tracking.
- Idempotency.
- Error handling.

↓

**Persistent Services**

- Database.
- Memory storage.
- Task scheduler.
- Activity history.
- Monitoring.
- Model/API integrations.

## Critical architectural principle

**The language model may propose an action, but the application must independently determine whether it is allowed and safe to execute.**

Do not rely only on system prompts to enforce permissions.

For example, before sending a message, the application should independently validate:

- The correct Telegram account and connection.
- The permitted target conversation.
- The user's active rules and settings.
- Whether approval is required.
- Whether the account owner has already responded.
- Whether automation has been paused.
- Whether the Telegram messaging window remains open.
- Whether the same action has already been executed.
- Whether the action is still relevant.

A model-generated request does not bypass application-level authorization.

---

# 9. Privacy, Security and Reliability Requirements

This product processes sensitive private communication.

Privacy and safety therefore belong in the original architecture, not in a future polish phase.

## Privacy and access

- Only process Telegram information the user has authorized the system to access.
- Follow Telegram developer terms and relevant data-processing requirements.
- Obtain required informed authorization before transmitting private message content to an external AI provider.
- Provide clear information about what the assistant can see, store and send.
- Define retention and deletion behavior.
- Separate every user's data and permissions.
- Store tokens, keys and account credentials securely.
- Avoid unnecessary collection of private information.
- Do not silently use unauthorized Telegram account access.
- Do not assume messages or channel content are universally accessible.

## Agent control

- Users must be able to pause or disable autonomous actions.
- The assistant should clearly distinguish drafts from messages actually sent.
- Sending permissions must be configurable.
- Sensitive or uncertain actions should require approval.
- Users should be able to review actions performed by the AI.
- The assistant should recognize human intervention and avoid conflicting responses.
- A failed action must never be reported as completed.
- Incoming messages from other people must be treated as untrusted content, not privileged instructions.

## Reliability

Plan for:

- Duplicate Telegram updates.
- Out-of-order events.
- API failures and timeouts.
- Background job persistence.
- Application restarts.
- Expired reply permissions.
- User edits or deletes.
- Human takeover during pending actions.
- Incorrect or incomplete model outputs.
- Uncertain conversation context.
- Retry safety and idempotency.
- Logging, monitoring and error reporting.

In particular, the assistant must not accidentally send duplicate responses or continue acting after the owner pauses automation.

---

# 10. Initial Product Scope and Prioritization

We discussed capability priorities as a preliminary product assessment.

## Core capabilities

- Working private Telegram AI assistant.
- Telegram business account connection.
- Authorized incoming DM observation.
- Persistent context and memory.
- Contextual DM summaries.
- Intelligent reply suggestions.
- User approval controls.
- Safeguarded autonomous responses.
- Basic notification and monitoring behavior.
- Action history and permission enforcement.

## Early expansion

- Contact-specific instructions.
- Intelligent inbox prioritization.
- Long-term memory.
- Follow-up tracking.
- Scheduled reminders.
- Conditional automations.
- Natural-language automation configuration.
- More advanced conversation management.

## Later investigation

- Channel information processing.
- Expanded Telegram information sources.
- Voice and file processing.
- Advanced semantic retrieval.
- More sophisticated tools.
- Additional third-party integrations.
- Telegram Mini App interface.
- Group features, if useful.
- Public subscriptions or monetization.

These priorities are provisional. The PRD should evaluate dependencies, user journeys and technical feasibility before establishing release boundaries.

**Do not interpret this as permission to reduce the whole product to an away assistant.**

The first implementation should prove the core architecture while preserving the broader vision.

---

# 11. Preliminary Milestones and Vertical Slices

We outlined the following tentative development milestones.

They need to be expanded into technically precise vertical slices with measurable acceptance criteria.

## M01 — Assistant Foundation

Create the Telegram bot and working backend.

Result: I can send the assistant a private Telegram message and receive an AI-generated response.

Include appropriate error handling and automated tests.

## M02 — Telegram Account Integration

Connect my Telegram account to the assistant using the official business bot integration.

Result: The backend receives authorized account message events and can identify their context and origin.

Validate the connection with real Telegram testing rather than mocked events alone.

## M03 — Context and Persistence

Persist authorized messages, user settings and relevant context.

Result: The agent understands retained conversation context and can produce meaningful summaries of information actually available to it.

## M04 — AI-Assisted Communication

Generate context-aware suggested responses.

Result: I can review, approve, edit or reject a proposed reply before it is sent.

Verify destination identity, permissions and action state.

## M05 — Autonomous Communication

Implement controlled, rule-based AI responses.

Result: The assistant can handle explicitly authorized conversations within Telegram's limits, with safeguards, logs and takeover behavior.

## M06 — Intelligent Inbox

Introduce useful DM summaries, prioritization, alerts and follow-up detection.

Result: The assistant can explain what happened, identify relevant developments, and distinguish messages requiring attention.

## M07 — Personalized Automation

Add persistent instructions, contact-specific rules, scheduled jobs and conditional actions.

Result: I can give an instruction such as "monitor this conversation and alert me when a specific condition occurs" and have the system execute and track it reliably.

## M08 — Expanded Agent Capabilities

Extend memory, retrieval, accessible Telegram information sources, tools and advanced automation.

Prioritize capabilities based on usefulness and supported API access.

## M09 — Public Product Readiness

Prepare reliable multi-user operation, onboarding, account isolation, quotas, observability, operational controls, privacy requirements and possible billing integration.

This is a preliminary sequence, not the final approved implementation plan.

## Vertical-slice methodology

Each slice should:

1. Deliver a coherent, demonstrable end-to-end capability.
2. Include only the infrastructure necessary for that capability.
3. Be testable independently.
4. Include appropriate automated tests.
5. Include real Telegram verification when needed.
6. Define success criteria before implementation.
7. Preserve existing behavior.
8. Avoid unnecessary architectural rewrites later.

Do not divide milestones purely by technical layer, such as "database first, APIs next, UI later," if that prevents us from verifying functional product behavior.

---

# 12. Development Workflow

We want a disciplined development process.

## Phase A — Product Requirements Document

Create a detailed `PRODUCT_REQUIREMENTS.md`.

It should cover:

- Product purpose and vision.
- Target users.
- Real user problems.
- Primary user journeys.
- Feature inventory.
- Functional requirements.
- Telegram-specific constraints.
- Agent behavior and autonomy levels.
- User permissions and approval flows.
- Memory and personalization.
- Monitoring and automation behavior.
- Error and exception handling.
- Privacy and security requirements.
- Nonfunctional requirements.
- Initial release scope.
- Future capabilities.
- Explicit exclusions.
- Acceptance criteria.
- Open product decisions.

Clearly separate established decisions from new recommendations.

Avoid specifying implementation details as though they were product requirements unless they genuinely affect the product experience.

## Phase B — Technical Architecture

Create `ARCHITECTURE.md`.

Cover:

- Final recommended technology stack.
- Alternative approaches and tradeoffs.
- Application modules and responsibilities.
- Telegram Bot API integration.
- Business connection handling.
- Agent orchestration.
- Tools and execution contracts.
- Authorization model.
- Data model.
- Memory system.
- Background jobs and scheduling.
- Event processing.
- Concurrency and idempotency.
- Security and privacy.
- Deployment.
- Testing.
- Monitoring.
- Operational failure handling.
- Multi-user extensibility.

Use diagrams where they materially improve technical clarity.

## Phase C — Implementation Plan

Create `IMPLEMENTATION_PLAN.md`.

Define milestones as end-to-end vertical slices.

For each milestone, include:

- Objective.
- User-visible result.
- Scope.
- Dependencies.
- Implementation components.
- API requirements.
- Data requirements.
- Major risks.
- Automated tests.
- Manual integration tests.
- Acceptance criteria.
- Definition of done.

Identify the highest-risk assumptions that should be validated earliest.

Especially prioritize a real business-connection proof of concept before building advanced workflows dependent on it.

## Phase D — Implementation

Once the requirements, architecture and implementation plan are ready for review, proceed with project setup and incremental development.

Use appropriate version control, modular source code, reproducible dependencies, secure configuration, automated tests and concise project documentation.

Do not write large amounts of speculative code before validating critical Telegram integration behavior.

## Phase E — Personal Testing

Use my actual Telegram account only with my explicit authorization and proper safety controls.

For testing message interactions, use a separate test account or controlled conversations where appropriate.

Validate:

- Correct account permissions.
- Reliable message receipt.
- Response generation.
- Approval workflows.
- Safe autonomous behavior.
- Summaries and memory.
- Alerts.
- Background jobs.
- Recovery from failures.
- Human takeover and pause controls.

## Phase F — Potential Public Launch

Only after personal testing and product validation, evaluate:

- Multi-user onboarding.
- Hosting and operational capacity.
- Model/API cost.
- Usage limits.
- Subscription design.
- Billing.
- Privacy disclosures.
- Retention and deletion controls.
- Abuse prevention.
- Production monitoring.
- Public release.

Monetization is a possibility, not a current requirement to implement billing.

---

# 13. Engineering Principles

Follow these principles throughout development.

**Build an agent, not a chatbot.** Real tool use, context, workflows, action permissions and reliable execution are central.

**Keep Telegram central.** Prioritize Telegram-specific value rather than drifting into an unrelated general-purpose assistant.

**Design for autonomy with user control.** Do not equate agent capability with unrestricted action-taking.

**Build incrementally.** Use working vertical slices, testing and integration validation.

**Avoid overengineering.** Introduce infrastructure only when warranted.

**Preserve extensibility.** We are building for one user first, but future public access should remain viable.

**Prioritize reliability over impressive demos.** A system that safely handles normal messages is more valuable than one that performs elaborate workflows unreliably.

**Distinguish technical feasibility from product desirability.** A capability should not automatically be implemented because Telegram supports it.

**Treat claims as testable.** Research, validate and document important SDK/API capabilities instead of relying on assumptions.

**Maintain a coherent product.** Avoid creating disconnected features that don't contribute to the overall assistant experience.

---

# 14. How to Work With Me

These collaboration instructions are important.

### Preserve the goal

Keep the project focused on building a genuinely useful Telegram-native personal AI agent.

Don't drift toward unrelated products, generic chatbot functionality, business CRM tools, or hypothetical features unless they materially serve the agreed goal.

### Do not misinterpret questions as decisions

When I ask whether something is possible, advisable, or better than another approach, I am asking for analysis.

I am not necessarily directing you to change our architecture, feature scope, or development plan.

Evaluate the options objectively, explain tradeoffs, and maintain existing decisions unless evidence justifies changing them.

### Challenge weak decisions

Do not automatically agree with my suggestions.

If an approach creates architectural problems, unnecessary complexity, policy risks, weak user experience or poor future extensibility, explain why and recommend a better alternative.

Likewise, do not defend an existing decision merely because it was previously made.

### Avoid unnecessary scope restrictions

A first release should be manageable, but do not arbitrarily shrink the product vision or prematurely discard important functionality.

Differentiate between:

- Long-term product vision.
- Initial release requirements.
- Implementation milestones.
- Later opportunities.
- Unsupported or inappropriate functionality.

### Research before making important technical decisions

Use current official documentation and relevant technical sources where necessary.

Compare viable approaches based on actual constraints.

Identify uncertain claims explicitly.

Avoid presenting assumptions or guesses as verified facts.

### Produce actionable technical work

I want implementation-ready documentation, not abstract brainstorming alone.

Define actual interfaces, workflows, states, acceptance criteria, architecture and implementation tasks at the appropriate stage.

### Control the planning-to-implementation transition

Work proactively on research, analysis and documentation.

Present major architectural and product choices clearly for review before committing to consequential implementation changes.

Once a milestone is approved, execute its agreed scope rather than repeatedly asking for confirmation about routine implementation details.

### Be direct and precise

Avoid repetitive agreement, unnecessary praise, generic introductions and excessive commentary.

Explain reasoning where important, challenge assumptions when warranted, and stay focused on the requested task.

---

# 15. Current Project State

**Completed conceptually:**

- Initial product vision.
- Target-user direction.
- Telegram-native interaction model.
- Combined assistant/bot and account integration concept.
- Preliminary feature exploration.
- Initial Telegram feasibility research.
- Initial competitive references.
- Proposed technology stack.
- High-level architecture.
- Preliminary development milestones.
- Initial security and reliability considerations.

**Not yet completed:**

- Formal Product Requirements Document.
- Final architecture specification.
- Detailed API permission and integration matrix.
- Final database and event models.
- Final selection of AI model/provider.
- Detailed vertical-slice specifications.
- Development environment setup.
- Repository/project initialization.
- Bot creation and token configuration.
- Real business-account connection validation.
- Code implementation.
- Automated test suite.
- Deployment.
- Personal beta testing.
- Public product validation.

No functioning bot or backend should be assumed to exist yet.

Do not claim that any implementation has already been completed.

---

# 16. Your Immediate Assignment

Continue the project from the completed exploratory discussion.

## Step 1 — Validate critical technical assumptions

Before finalizing the requirements or architecture, check current authoritative Telegram documentation and relevant SDK support for the features on which the initial product depends.

Focus particularly on:

- Business bot connection and permissions.
- Available account-message events.
- Sending messages on behalf of the account.
- The 24-hour restriction and other relevant limits.
- User configuration and connection lifecycle.
- Human takeover behavior and event handling.
- Current aiogram compatibility.
- Channel access constraints.
- Applicable Telegram developer and privacy requirements.

This is targeted validation, not a request to repeat all previous research unnecessarily.

Document any corrections to previous findings and their practical implications.

## Step 2 — Produce the formal PRD

Use the product vision and capability inventory above.

Analyze the real user workflows and define a sensible initial release without reducing the product to a mere autoresponder.

Differentiate established product decisions from recommendations and unresolved questions.

Include measurable acceptance criteria and edge cases.

## Step 3 — Produce the technical architecture

Assess the proposed stack, choose appropriate technical patterns, define components, data models, agent capabilities, execution policies and infrastructure.

Design for a reliable personal prototype and eventual multi-user expansion.

## Step 4 — Produce the milestone and vertical-slice plan

Convert the PRD and architecture into a sequenced development plan with clear deliverables, dependencies, tests, and definitions of done.

Surface the riskiest feasibility assumptions early.

## Step 5 — Prepare for implementation

Recommend the exact first engineering milestone and identify anything genuinely needed from me, such as creating a Telegram bot, supplying credentials securely, choosing an AI provider, or authorizing account integration.

Do not ask me to supply information that can be independently researched or reasonably decided from existing context.

Do not request secrets directly in chat when secure configuration is available.

## Expected deliverables

Produce a coherent set of development documents:

1. `PRODUCT_REQUIREMENTS.md`
2. `ARCHITECTURE.md`
3. `IMPLEMENTATION_PLAN.md`

Use additional supporting documents only when they serve a clear purpose, such as a verified Telegram API feasibility matrix or an architectural decision record.

The documents must be consistent with each other.

**End goal:** A properly planned, implementation-ready Telegram-native personal AI agent project, with a clear route from the first functioning vertical slice to a reliable personal assistant and eventually a public product.

Start with the targeted technical validation and product requirements work. Carry the project forward using the full context above rather than reopening decisions we have already settled.