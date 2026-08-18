# Coding LLM Router Domain Context

This context defines the domain language for routing coding-agent requests across configured, protocol-compatible upstreams. It separates request semantics from provider availability and task outcome.

## Routing

**Provider**:
A configured upstream connection with one protocol, base URL, credential scope, and concurrency limit. A Provider is an operational endpoint, not necessarily a vendor company.
_Avoid_: vendor, model, account (unless the credential scope is specifically meant).

**Model Target**:
A configured upstream model alias resolved to one Provider, protocol, capability set, and model identity. A Model Target is the smallest selectable routing destination.
_Avoid_: provider, model name (the target includes both the provider and upstream model).

**Route Profile**:
A stable client-facing routing intent such as `code/fast`, `code/balanced`, `code/deep`, or `code/auto`. A profile resolves to an ordered set of Model Targets for the inbound protocol.
_Avoid_: client model, upstream model.

**Execution Plan**:
An immutable, ordered decision produced by the Routing Kernel. It contains the selected primary target, bounded fallbacks, timeouts, route reasons, and policy version; it does not perform network calls.
_Avoid_: retry plan (a plan can contain fallbacks, but retry policy is only one part of it).

## Availability

**Health State**:
The current operational eligibility of a Provider or Model Target: `healthy`, `cooldown`, `half_open`, or `blocked`.
_Avoid_: quality score, model quality (health describes reachability and acceptance, not generated-answer quality).

**Failure Domain**:
The smallest configured scope that should be suppressed after a failure. Provider failures suppress a Provider and its targets; target failures suppress only one Model Target.
_Avoid_: error scope, outage group.

**Availability Snapshot**:
An immutable, timestamped view of target eligibility supplied to the Routing Kernel. It is an input to a deterministic routing decision, not mutable health state.
_Avoid_: health cache, live health check.

**Recovery Probe**:
The single ordinary request admitted after a cooldown to test whether a suppressed failure domain has recovered. The Router does not generate synthetic upstream probes in v0.3.
_Avoid_: ping, active health check.

**Attempt Outcome**:
A sanitized classification of one upstream execution, including success, transient failure, permanent failure, cancellation, or post-commit stream failure. It is used for health updates and telemetry, not task-quality judgment.
_Avoid_: task result, model score.

## Feedback And Evaluation

**Outcome Signal**:
A transient, conservative success or failure indication extracted from protocol input such as an explicit tool result. It may influence the current routing context but is not a durable evaluation label.
_Avoid_: Outcome Event, task result.

**Outcome Event**:
An immutable, authenticated, bounded piece of evidence about the observed result of a prior routed request. It is evaluation data, not Provider health and not a command to change routing.
_Avoid_: Attempt Outcome, quality score, reward.

**Task ID**:
An optional client-generated opaque UUID that groups related routed requests and Outcome Events. It carries no task description and does not affect routing.
_Avoid_: session ID, request ID, task name.

**Route Decision Input**:
A sanitized immutable record of the request facts, session snapshot, availability snapshot, and policy identity required to rerun one historical routing decision.
_Avoid_: prompt snapshot, request body.

**Routing Policy Snapshot**:
A sanitized, versioned description of model targets, profiles, thresholds, and routing rules, excluding credentials and upstream connection details.
_Avoid_: full configuration, Provider configuration.

**Replay Result**:
A hypothetical Execution Plan produced by evaluating a Route Decision Input with a selected Routing Policy Snapshot. It is not a claim about the answer, cost, latency, or Outcome that the hypothetical target would have produced.
_Avoid_: prediction, quality verdict, counterfactual Outcome.
