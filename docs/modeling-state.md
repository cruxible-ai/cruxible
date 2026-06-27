# Modeling State in Cruxible: Gates, Tags, and Flags

Cruxible holds durable, shared state that both people and AI agents work from.
When you decide what to put in that state, every piece of information you add
plays one of three roles. You don't label the role directly — it's defined by
**what Cruxible does with the information, and when.**

Getting these roles right is the heart of good modeling. It's also what keeps
the system trustworthy without making it rigid.

## The three moments

An agent working with Cruxible moves through three moments, and each role acts in
exactly one of them:

1. **Reading** — before it acts, an agent reads the current state into its
   working view. *What it sees here shapes what it does.*
2. **Saving** — when the agent makes a change, that change has to be saved back.
   *What's allowed to be saved is what's guaranteed.*
3. **Maintaining** — separately, on a regular cadence, you check the health of
   the state and tidy it up. *What gets surfaced here is what needs attention.*

The three roles map one-to-one onto these moments.

## The three roles

| Role | What it means | When it acts | How it's set up | Does it block? |
| --- | --- | --- | --- | --- |
| **Tag** | Helps you find, filter, and group things — changes what you *see* | Reading | named queries | No — purely informational |
| **Gate** | A rule Cruxible enforces — changes what's *allowed* | Saving | mutation guards, constraints | **Yes** — refuses the save if the rule isn't met |
| **Flag** | A health check that points out problems to fix later — changes what's *surfaced for cleanup* | Maintaining | quality checks (warning or error) | No — it reports, never blocks |

### Tags — what you see

A tag is a way of organizing. "Which area does this work belong to?" "Group these
by priority." Tags make state findable and let an agent pull the right things
into view. They never stop anyone from doing anything — they just shape what
shows up. *Most* of what you model is tags, and that's healthy.

### Gates — what's allowed

A gate is a rule the system actually enforces at the moment a change is saved.
For example: *a piece of work can't be marked "done" until an approved review is
attached to it.* If the rule isn't satisfied, the save is **refused** — not
warned about, refused. Gates are how Cruxible makes promises that hold no matter
who, or which agent, is doing the work. They are powerful, and they have a cost:
every gate is one more rule to satisfy, so it adds a little friction to every
change it touches.

### Flags — what needs attention

A flag is a health check that runs when you *review* the state, not when a change
is saved. For example: *every work item should be linked to a product area.* If
one isn't, the review surfaces it as something to clean up — but nothing was
blocked when that work item was created. Flags come in two strengths: a
**warning** (a gentle nudge) and an **error** (a louder "this really should be
fixed"). Either way, a flag points; it never stops.

## How to decide which one you need

When you add something to the model, ask one question: **what should happen if
this isn't right?**

- **Nothing should be allowed to proceed → make it a Gate.** Use a gate only when
  breaking the rule would cause real harm, because gates add friction to every
  change. Reserve them for the promises that genuinely matter.
- **It should be caught and fixed later, but not block work now → make it a
  Flag.** Good for hygiene and consistency you care about but don't need to
  enforce in the moment.
- **Nothing needs enforcing — you just want to find and group it → make it a
  Tag.** This is the default, and most things land here.

A useful rule of thumb: **a thing earns heavier treatment — its own type, its own
gates — only when you enforce a real rule over it.** If you only ever filter or
group by something, a simple tag is the right, low-cost choice. Promoting
everything to a gate makes the system rigid and tiring to use; leaving real
guarantees as mere tags makes it untrustworthy. The skill is matching the role to
the stakes.

## An example

Say you're tracking project work:

- *"Group work items by the product area they touch."* → **Tag.** It organizes;
  it enforces nothing.
- *"A work item can't be closed until an approved review is attached."* →
  **Gate.** Try to close one without a review and the save is refused.
- *"Every work item should be linked to a product area."* → **Flag.** If one
  isn't, your next health check lists it — but it was never blocked from being
  created.

Same three pieces of information, three different roles, because you want three
different things to happen when each one isn't right.

---

*Related, and a separate topic: this guide covers how state is **read and
enforced** once it's there. How state is allowed **in** — direct writes versus
governed proposals, evidence requirements, and review — is its own dimension,
covered in the resolution and governance docs.*
