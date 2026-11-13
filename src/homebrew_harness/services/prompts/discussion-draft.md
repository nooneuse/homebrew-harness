Draft a concise community reply from the supplied Lead assessment and source discussion.
The Lead has already chosen facts, scope, next_step and questions. Do not change them or invent new
requirements. Ask only the open requester questions in public_questions; include their IDs in
communication.question_ids. Do not ask the participant to choose document placement, provision CI,
assign implementers or approve internal roadmap decisions. Do not repeat answered questions or
require someone to provide a test machine.

If there are no public questions, summarize what is understood and the next appropriate step
without adding questions. Follow the host's writing preferences; keep job IDs, internal operator
notes and repetitive runtime disclaimers out of the public reply.

Submit only {communication: {...}} through service_propose; communication may be null when no
public response is useful. It remains a candidate requiring the existing publication authorization.

Call service_propose by itself for the final reply candidate. Successful phase acceptance ends drafting; no extra acknowledgement is needed. Other tools may be used in earlier calls when needed.
