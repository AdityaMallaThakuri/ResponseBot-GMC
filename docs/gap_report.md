# Gap report: gold set v1

Source: 200 threads labelled by hand (`eval/data/gold_set.csv`), a stratified sample, not a random one. Counts show what the sample contains, not how common a label is overall. No lead names, emails or message text appear below.

## (a) Labels with fewer than 8 examples (too few to measure)

| Label | Examples | Can we get more from this dataset? |
|---|---|---|
| trial_request | 3 | No. The draft run found only 3 in all 1,163 threads. |
| objection_price | 4 | No. The draft run found only 6 in all 1,163 threads. |
| meeting_request | 5 | Yes. The draft run found 84, so label more of them. |
| price_and_info | 5 | Yes. The draft run found 17. |

price_question, booked_confirmation and objection_info sit at exactly 8, the minimum. Also, the template column is blank in all 200 rows, so template choice cannot be scored yet. The verdict column is 183 pass, 16 flag and 1 hard_dq, so it barely tests anything yet.

## (b) Where Smartlead's category and our label disagree

Smartlead agreed with us on 86 of the 163 rows whose category has a matching label of ours (Not Interested, Do Not Contact, Out Of Office, Wrong Person, Automated Response, Booked, Lead Magnet Follow Up). Patterns, biggest first:

1. **Booked tag, but the first reply was something else (17 of 24 Booked rows).** Smartlead tags the outcome of the thread. We label the first reply (a free-list ask, a price question and so on). Both are right. Do not use "Booked" as proof of a booking confirmation.
2. **"Not Interested" for people who want no more contact (15 of 63).** We labelled these do_not_contact.
3. **"Not Interested" for people who are still engaged (16).** They raised an objection (8) or asked for a list, trial, price or call (8).
4. **People who left, retired, merged or stopped using the inbox, filed as Do Not Contact (6), Out Of Office (7) or Automated Response (3).** We labelled all 16 wrong_person, following the rule that a permanent departure or an unmonitored inbox is a referral.
5. **Counteroffers filed as "Not Interested" (6).** The lead declines but proposes a commission, an intro fee, pay per conversation or a partnership. We labelled them off_script, so a human reads them.
6. **"Positive response but no reply" (27 rows).** It has no matching label. These spread over 9 of our labels, mostly info_request and free_list_request.
7. **Small groups:** automatic notices tagged Do Not Contact or Out Of Office (4), an empty reply tagged Do Not Contact (1), a hostile reply tagged Wrong Person (1).

## (c) Decisions for Aryan or Ray

1. **Counteroffers (pattern 5).** All 6 went to a human. Should they stay human-only, get a playbook reply, or become their own label?
2. **Sensitive replies.** Two threads, one mentioning a death and one a hospice, were marked "human only" in the notes. Confirm these never get a draft.
3. **One do_not_contact label, two suppression scopes.** The rules separate campaign suppression from a global opt-out (legal threat, spam complaint, "remove me from all lists"). Should the label split, or will a human decide the scope in Slack?
4. **Final verdict rules.** The current rules are working definitions awaiting your final ones. With 1 hard_dq in 200, they do not separate leads yet.
5. **trial_request and objection_price.** The dataset has too few to ever reach 8. Accept them as unmeasured for now, or write test cases by hand?
6. **Templates.** The gold set has no template labels. Label them by hand, or derive them from the message type?
7. **Smartlead categories 118216 (Lead Magnet Follow Up) and 98680 (PR Last 2 Weeks).** Both are marked unknown. "Lead Magnet Follow Up" matched our lead_magnet_request on all 15 rows. Is it a reply label or an automation trigger?
