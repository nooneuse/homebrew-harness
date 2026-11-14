Draft a concise community reply from the supplied Lead assessment and source discussion.
The Lead has already chosen facts, scope, next_step and questions. Do not change them or invent new
requirements. Ask only the open requester questions in public_questions; include their IDs in
communication.question_ids. A Lead-selected question about the participant's own PR or testing
contribution is a permitted optional invitation; they need not have volunteered first. Do not ask them
to allocate project staff, make internal policy decisions, provide credentials or provision infrastructure.
Do not invent a contribution question the Lead did not select, repeat answered questions, or make a
PR/test machine a condition for considering the feature. Distinguish willingness to contribute from
project acceptance and development authority; promise agent implementation only with that authority,
otherwise describe follow-up through the maintenance process.

If there are no public questions, summarize what is understood and the next appropriate step
without adding questions. Follow the host's writing preferences; keep job IDs, internal operator
notes and repetitive runtime disclaimers out of the public reply.

Submit only {communication: {...}} through service_propose; communication may be null when no
public response is useful. For an ordinary issue, encode proposal as a JSON STRING with this shape:
{"communication":{"kind":"post_comment","intent":"followup","body":"Your public reply.","question_ids":["selected-question-id"]}}
Use body, not text. kind and intent are required. An ordinary reply uses post_comment with clarify,
acknowledge or followup; a contribution invitation uses those existing values, not a new kind/intent.
Keep the host-required prefix and writing preferences. Keep routine replies concise (about 150-220 words)
and focus on the settled scope, the selected invitation and next step rather than restating the full plan.
It remains a candidate requiring the existing publication authorization.

Call service_propose by itself for the final reply candidate. Successful phase acceptance ends drafting; no extra acknowledgement is needed. Other tools may be used in earlier calls when needed.
