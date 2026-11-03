# Playbook proposal (NOT APPROVED)

Wording mined from Gamic's past replies, for Aryan or Ray to review. Nothing here is in
`app/config/playbook.yaml`, which stays empty until wording is approved. Every claim still has
to be added to `app/config/facts.yaml` and approved there.

## How to read this

- Source: Gamic's real reply to each lead's first reply, from the GMC dataset. The label of the
  lead's reply is the human label where one exists (the gold set), otherwise the model's draft
  label. Model labels are noisy, so a version built mostly from them is marked UNSURE.
- A version is a group of replies with at least half of their word triples in common. The biggest
  groups per label are shown. The wording shown is the most typical member, copied exactly.
- Redactions: lead first names are `{NAME}`, last names `[LAST NAME]`, emails `[EMAIL]`, phone
  numbers `[PHONE]`. Words that differ in at least 30% of the group are replaced by a slot
  marker: `{TIME?}` looks like days and times, `{NUMBER?}` is a price, percentage or count
  that differs between replies (so it is NOT a fixed claim), and `{VARIES?}` is anything else
  that varies (often a company, a sector or a personal line). A question mark means a guess.
- A label with no recurring wording shows no version: one-off replies can carry a lead's
  personal details. For price_and_info the closest wording is under price_question (T4).
- `Also seen under` lists other labels whose replies use nearly the same wording. That is
  either label noise (the model's draft labels are imperfect) or one reply used for several
  situations: either way it is a question for Aryan or Ray.
- Company names are not detected automatically. A company or sector may remain inside fixed
  wording: read every version before approving it.
- Sub-templates (T2a, T2b, T2c, T3a, T4a, T4b, T5b) cannot be told apart from the label alone.
  Where a label has several, the versions below are candidates: which is which needs a decision.
- The calendar link and any numbers are listed as claims so they can be approved one by one.

## Summary

| label | template | real replies | human-labelled | recurring versions found |
|---|---|---|---|---|
| meeting_request | T1 | 79 | 5 | 1 |
| info_request | T2 | 167 | 9 | 14 |
| free_list_request | T3 | 87 | 17 | 8 |
| price_question | T4 | 30 | 8 | 4 |
| price_and_info | T5 | 16 | 5 | 0 |
| lead_magnet_request | T6 | 61 | 16 | 6 |
| trial_request | T7 | 2 | 2 | 1 |
| booked_confirmation | T8 | 6 | 6 | 0 |
| objection_info | T2a, T2b or T2c | 3 | 3 | 0 |
| objection_price | T4a or T4b | 2 | 2 | 0 |

## Versions

### T1: meeting_request

79 real replies (5 with a human label, 74 with the model's draft label).

#### Version A: 3 of 79 replies (4%)

Campaigns: GMC - M&A June (US/Canada) (1); GMC - PE Aug (A Sector Named) (1); GMC - M&A May (1)
Also seen under: info_request (10 replies); free_list_request (43 replies)

```text
We use AI triggers and factors such as how long they’ve been in a certain role like COO, revenue and EBITDA range, age of the owner, and more.

It’s a combination of factors we fine-tune specifically for you.

To get the list built properly, I’d like to hop on a quick call to get more info about what you’ve got going on and your deal box I don’t want to just send over a crappy, generic list.

Would {VARIES?} or {TIME?} work for you?

If those don’t work, just grab a time over here.
```

Slots: `VARIES?`, `TIME?`

Claims in the fixed wording: none found

UNSURE: most rows carry the model's draft label, not a human label.

### T2: info_request

167 real replies (9 with a human label, 158 with the model's draft label).

#### Version A: 17 of 167 replies (10%)

Campaigns: GMC - M&A March New (6); POS REPLY (3); GMC - M&A June (US/Canada) (2); GMC - M&A 24/02 (2)

```text
At a high level, we act as a fractional deal origination team.

We identify owner-led businesses that match your industry, size, and geography, focusing on owners showing early signs of a transition, and handle discreet, relationship-first outreach on your behalf.

The goal is not volume. It’s to surface a small number of credible, under-the-radar owners who are open to a conversation.

If this makes sense, happy to walk through the process in more detail.

Would {VARIES?} at {TIME?} or {TIME?} at {TIME?} work {VARIES?}

If those {VARIES?} work, {VARIES?} grab a {VARIES?} here.
```

Slots: `VARIES?`, `TIME?`

Claims in the fixed wording: none found

UNSURE: most rows carry the model's draft label, not a human label.

#### Version B: 14 of 167 replies (8%)

Campaigns: GMC - M&A (Free Pilot) V3 (6); GMC - M&A (Free Pilot) V1 (5); GMC - M&A (Free Pilot) V2 (3)
Also seen under: trial_request (2 replies)

```text
Hey {NAME},

Sounds good, we've only got 2 weeks so I want to make sure you get the most out of it.

To kick this off properly I'd need 15-20 minutes to get your deal box, industries you work in, EBITDA range, geography, some info on what's worked well for you historically and what hasn't, etc.

This helps me fine tune our targeting so that over the next 2 weeks you're not just getting in front of any old leads, you're speaking to the people who actually need and want to have the conversation with you.

Would tomorrow or {TIME?} at {TIME?} or {TIME?} work?

If not just grab a time over here.
```

Slots: `TIME?`

Claims in the fixed wording: `…Sounds good, we've only got 2 weeks so I want to make sure…`; `…ck this off properly I'd need 15-20 minutes to get your deal box,…`; `…rgeting so that over the next 2 weeks you're not just getting…`

UNSURE: most rows carry the model's draft label, not a human label.

#### Version C: 11 of 167 replies (7%)

Campaigns: GMC - M&A Aug (A Sector Named) (2); GMC - MA NAMED 01/09 (2); GMC - IB BROAD 01/09 (1); GMC - M&A July (US/Canada) (1)
Also seen under: free_list_request (17 replies)

```text
Hey {NAME},

We use AI triggers and factors such as how long {VARIES?} owner {VARIES?} been {VARIES?} the {VARIES?} revenue and EBITDA range, age of the owner, and more. It is a combination of factors we fine-tune specifically for you.

To get the list built properly, I would like to hop on a quick call to get more info about what you have got going on and your deal box. I do not want to just send over a crappy, generic list.

Would tomorrow or {TIME?} at {TIME?} or {TIME?} CST work?

If not, just grab a time over here.
```

Slots: `VARIES?`, `TIME?`

Claims in the fixed wording: none found

UNSURE: most rows carry the model's draft label, not a human label.

### T3: free_list_request

87 real replies (17 with a human label, 70 with the model's draft label).

#### Version A: 26 of 87 replies (30%)

Campaigns: GMC - M&A May (13); GMC - M&A (UK) (7); GMC - M&A March New (2); GMC - MA BROAD 01/09 (1)
Also seen under: meeting_request (3 replies); info_request (10 replies)

```text
Hey {NAME},

We use AI triggers and factors such as how long they’ve been in a certain role like COO, revenue and EBITDA range, age of the owner, and more.

It’s a combination of factors we fine-tune specifically for you.

To get the list built properly, I’d like to hop on a quick call to get more info about what you’ve got going on and your deal box… I don’t want to just send over a crappy, generic list.

Would {VARIES?} at {TIME?} or {NUMBER?} work?

If those don’t work, just grab a time over here.
```

Slots: `VARIES?`, `TIME?`, `NUMBER?`

Claims in the fixed wording: none found

UNSURE: most rows carry the model's draft label, not a human label.

#### Version B: 17 of 87 replies (20%)

Campaigns: GMC - M&A TAM (A Sector Named) (3); GMC - MA BROAD 25/09 (3); GMC - MA BROAD 01/09 (2); GMC - PE (Deal Origination) (2)
Also seen under: meeting_request (3 replies); info_request (11 replies)

```text
Hey {NAME},

We use AI triggers and factors such as how long {VARIES?} owner {VARIES?} been {VARIES?} a {VARIES?} like {VARIES?} or {VARIES?} revenue and EBITDA range, age of the owner, and more. It is a combination of factors we fine-tune specifically for you.

To get the list built properly, I would like to hop on a quick call to get more info about what you have got going on and your deal box. I do not want to just send over a crappy, generic list.

Would {VARIES?} or {TIME?} at {TIME?} or {TIME?} CST work?

If not, just grab a time over here.
```

Slots: `VARIES?`, `TIME?`

Claims in the fixed wording: none found

UNSURE: most rows carry the model's draft label, not a human label.

#### Version C: 5 of 87 replies (6%)

Campaigns: GMC - PE June (US/Canada) (4); GMC - PE (Deal Origination) (1)
Also seen under: lead_magnet_request (2 replies)

```text
Hey {NAME},

Appreciate your patience here, we had some internal systems changes hence the delay here.

You can find a link to the doc we made for you over here.

For the list of 15-20 businesses showing exit signals I promised.. we use AI triggers and factors such as how long a founder has been running their business, revenue and EBITDA range, ownership structure, industry, and other signals that indicate a business owner may be approaching a liquidity event or transition.

It is a combination of factors we fine-tune specifically around your deal box.

To get the list built properly, I would like to hop on a quick call to get more info about what you have got going on and your acquisition criteria. I do not want to just send over a crappy, generic list.

Would tomorrow at {TIME?} or friday at {TIME?} CST work?

If not, just grab a time over here.

Best,

Aryan

CEO | Gamic Media
```

Slots: `TIME?`

Claims in the fixed wording: `…u over here. For the list of 15-20 businesses showing exit signa…`

UNSURE: most rows carry the model's draft label, not a human label.

### T4: price_question

30 real replies (8 with a human label, 22 with the model's draft label).

#### Version A: 6 of 30 replies (20%)

Campaigns: GMC - M&A June (US/Canada) (3); GMC - M&A July (US/Canada) (2); GMC - M&A May (1)

```text
Hey {NAME},

Pricing depends. I would like to learn more about the type of deals you go after as well as the EBITDA and revenue ranges of the companies you work with. Then I will be able to put together a specific proposal based off of that.

But to give you an idea we work on a hybrid model of a {VARIES?} retainer to {VARIES?} and {VARIES?} plus a success fee {VARIES?} to {VARIES?} The fastest way to get you exact numbers is just a quick call.

Would {VARIES?} at {TIME?} or {TIME?} at {TIME?} CST work?

If not, just grab a time here.

{VARIES?}
```

Slots: `VARIES?`, `TIME?`

Claims in the fixed wording: none found

UNSURE: most rows carry the model's draft label, not a human label.

#### Version B: 3 of 30 replies (10%)

Campaigns: GMC - Video (3)

```text
{VARIES?} {NAME},

It varies between clients but typically our clients pay anywhere from {NUMBER?} for every {NUMBER?} meetings we get them.

This usually works out to a 2–5X ROI depending on their offer and pricing.

We also helped First Rank, an SEO and PPC agency, book 75+ meetings and close over 13 deals with this.

I can give you an exact quote after learning more about your business on a quick call.

Would {VARIES?} at {TIME?} or {TIME?} at {TIME?} work for you?

{VARIES?} work, {VARIES?} a {VARIES?}
```

Slots: `VARIES?`, `NUMBER?`, `TIME?`

Claims in the fixed wording: `…This usually works out to a 2–5X ROI depending on their off…`; `…, an SEO and PPC agency, book 75+ meetings and close over 13 d…`; `…k 75+ meetings and close over 13 deals with this. I can give…`

UNSURE: capitalised words to check for a person or company name: First, Rank, SEO, PPC; most rows carry the model's draft label, not a human label; one campaign only.

#### Version C: 2 of 30 replies (7%)

Campaigns: GMC - M&A May (2)

```text
Hey {NAME}.. the list we'll put together for free and send it over so you can make sure it's up to scratch.

For our full done for you services investment varies based on scope and targeting, but for most M&A firms it typically falls anywhere between 10-20% of your advisory fee.

That covers lead sourcing, filtering, outreach, and qualification. It functions as a lightweight, fractional origination layer rather than a traditional lead gen vendor. Which means we don't just send any and every lead your way.. instead we actually place a human representative on your campaigns that'll pre-engage and pre-qualify your leads.. meaning only good candidates make it to your desk.

I can give you a more exact number and tailored offering once I understand your deal criteria and volume expectations.

Would tomorrow at {TIME?} or wednesday at {TIME?} CST work for a quick call?

If those times are no good just grab one ovetr here.
```

Slots: `TIME?`

Claims in the fixed wording: `…lly falls anywhere between 10-20% of your advisory fee. That c…`; `…ically falls anywhere between 10-20% of your advisory fee. That…`

UNSURE: only 2 replies share this wording; most rows carry the model's draft label, not a human label; one campaign only.

### T5: price_and_info

16 real replies (5 with a human label, 11 with the model's draft label).

UNSURE: no wording recurs. These replies look individually written, so none is shown: a one-off reply can carry a lead's personal details. The closest family may be the wording under another template (see the notes at the top).

### T6: lead_magnet_request

61 real replies (16 with a human label, 45 with the model's draft label).

#### Version A: 28 of 61 replies (46%)

Campaigns: GMC - MASS LEAD MAGNET (12); GMC - Marketing (Lead Magnet) Canada (7); GMC - Competitor Engagement Monitor (5); GMC - Marketing (Lead Magnet) UK (4)
Also seen under: info_request (4 replies)

```text
Hey {NAME},

Great to hear back from you - here's the outreach strategy we put together for {VARIES?} https://drive.google.com/file/d/1snwx0z51uKwNCoBLABQ-UraNCHLHHUQgscKvm5ehOWE

Would love to walk you through the different methods and discuss how you could bring them to life.

How does tomorrow at {TIME?} or {TIME?} at {TIME?} ct sound?

If neither of those are good, please feel free to find some time here

Best,

Aryan

CEO | Gamic Media
```

Slots: `VARIES?`, `TIME?`

Claims in the fixed wording: `…trategy we put together for https://drive.google.com/file/d/1snwx0z51uKwNCoBLABQ-UraNCHLHHUQgscKvm5ehOWE Would love to walk you throu…`

UNSURE: contains 1 link(s) other than the calendar link, which the facts file would have to approve: https://drive.google.com/file/d/1snwx0z51uKwNCoB; most rows carry the model's draft label, not a human label.

#### Version B: 14 of 61 replies (23%)

Campaigns: GMC - Marketing (Lead Magnet) Canada (6); GMC - MASS LEAD MAGNET (5); GMC - Marketing (Lead Magnet) UK (3)
Also seen under: info_request (4 replies)

```text
Hey {NAME},

Great to hear back from you - here's the outreach strategy we put together for {VARIES?} https://drive.google.com/file/d/1WHYR9zLfPs3BAb_xHU6NCi2OtrF6-y_3Udq4wZnNXsA

Would love to walk you through the different methods and discuss how you could bring them to life.

How does {VARIES?} at {TIME?} or {TIME?} at {NUMBER?}

If neither of those {VARIES?} please feel free to find some time here (https://calendly.com/gamicmedia/one-one?month=2026-03&date=2026-03-25).
```

Slots: `VARIES?`, `TIME?`, `NUMBER?`

Claims in the fixed wording: `…trategy we put together for https://drive.google.com/file/d/1WHYR9zLfPs3BAb_xHU6NCi2OtrF6-y_3Udq4wZnNXsA Would love to walk you throu…`; `…free to find some time here (https://calendly.com/gamicmedia/one-one?month=2026-03&date=2026-03-25).…`; `….com/gamicmedia/one-one?month=2026-03&date=2026-03-25).…`; `…ia/one-one?month=2026-03&date=2026-03-25).…`; `…ne?month=2026-03&date=2026-03-25).…`

UNSURE: contains 1 link(s) other than the calendar link, which the facts file would have to approve: https://drive.google.com/file/d/1WHYR9zLfPs3BAb_; most rows carry the model's draft label, not a human label.

#### Version C: 4 of 61 replies (7%)

Campaigns: GMC - Marketing (Lead Magnet) UK (3); GMC - MASS LEAD MAGNET (1)
Also seen under: info_request (4 replies)

```text
Hey {NAME},

Great to hear back from you - here's the outreach strategy we put together for {VARIES?}

https://drive.google.com/file/d/10snguJ12QJMGFAns8qV8HIpA5ny7sC_SCmWbvRoSv-k

Would love to walk you through the different methods and discuss how you could bring them to life.

How does next Monday at {TIME?} PM or next {TIME?} at {TIME?} PM {VARIES?} for you?

{VARIES?} to {VARIES?}
```

Slots: `VARIES?`, `TIME?`

Claims in the fixed wording: `…rategy we put together for https://drive.google.com/file/d/10snguJ12QJMGFAns8qV8HIpA5ny7sC_SCmWbvRoSv-k Would love to walk you throu…`

UNSURE: contains 1 link(s) other than the calendar link, which the facts file would have to approve: https://drive.google.com/file/d/10snguJ12QJMGFAn; most rows carry the model's draft label, not a human label.

### T7: trial_request

2 real replies (2 with a human label, 0 with the model's draft label).

#### Version A: 2 of 2 replies (100%)

Campaigns: GMC - M&A (Free Pilot) V1 (2)
Also seen under: info_request (14 replies)

```text
Hey {NAME},

Sounds good, we've only got 2 weeks so I want to make sure you get the most out of it.

To kick this off properly I'd need 15-20 minutes to get your deal box, industries you work in, EBITDA range, geography, some info on what's worked well for you historically and what hasn't, etc.

This helps me fine tune our targeting so that over the next 2 weeks you're not just getting in front of any old leads, you're speaking to the people who actually need and want to have the conversation with you.

Would {TIME?} at {TIME?} or {TIME?} at 4pm CST work?

If not just grab a time over here.
```

Slots: `TIME?`

Claims in the fixed wording: `…Sounds good, we've only got 2 weeks so I want to make sure…`; `…ck this off properly I'd need 15-20 minutes to get your deal box,…`; `…rgeting so that over the next 2 weeks you're not just getting…`

UNSURE: only 2 replies share this wording; one campaign only.

### T8: booked_confirmation

6 real replies (6 with a human label, 0 with the model's draft label).

UNSURE: no wording recurs. These replies look individually written, so none is shown: a one-off reply can carry a lead's personal details. The closest family may be the wording under another template (see the notes at the top).

### T2a, T2b or T2c: objection_info

3 real replies (3 with a human label, 0 with the model's draft label).

UNSURE: no wording recurs. These replies look individually written, so none is shown: a one-off reply can carry a lead's personal details. The closest family may be the wording under another template (see the notes at the top).

### T4a or T4b: objection_price

2 real replies (2 with a human label, 0 with the model's draft label).

UNSURE: no wording recurs. These replies look individually written, so none is shown: a one-off reply can carry a lead's personal details. The closest family may be the wording under another template (see the notes at the top).
