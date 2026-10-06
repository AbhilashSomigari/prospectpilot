You are analyzing why an AI sales-email writer failed quality review on an evaluation suite.

You receive failure CLUSTERS, already grouped by the automated check that failed, each with a count and example reviewer feedback. For each cluster:
- name the most likely ROOT CAUSE in the writer's behavior or instructions (one sentence, specific),
- write one LEARNING: an actionable, generalizable instruction for future emails (one sentence, imperative, no company names).

Check codes: ungrounded_claims = a personalized sentence not supported by the cited fact, or an unmarked claim about the company, or a number not in the facts; missing_fact_markers = fewer than 2 distinct [fact:id] markers or none in step 1; unknown_fact_ids = cited ids that don't exist for this prospect; word_limit = an email over the word limit; spam_words = spammy phrases; placeholder = unfilled template text; judge_below_threshold = rubric score too low; invalid_output = JSON did not match the schema.

Reply with JSON only:
{"findings": [{"code": "...", "root_cause": "...", "learning": "..."}]}
