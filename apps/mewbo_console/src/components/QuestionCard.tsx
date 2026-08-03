import { useMemo, useState } from 'react';
import {
  CheckCircle2,
  Loader2,
  MessageCircleQuestion,
  MinusCircle,
} from 'lucide-react';
import type {
  AnswerDelivery,
  QuestionAnswerItemPayload,
  QuestionMeta,
  UserQuestionItem,
} from '../types';
import type { AnswerQuestionResult } from '../api/contracts';
import { FOCUS_RING } from './ui/focus-ring';
import { Input } from './ui/input';
import { Textarea } from './ui/textarea';

/** Client-side ceiling on the notes box, mirroring the core cap so an
 *  over-long note is prevented at the keyboard instead of bounced as a 422. */
const MAX_NOTES_CHARS = 4000;

interface QuestionCardProps {
  question: QuestionMeta;
  /**
   * Submit the answers, plus the group-level free-text `notes` when the user
   * typed any (blank ⇒ omitted, never an empty string). Resolves to a
   * structured outcome so the card can settle silently when the question was
   * resolved elsewhere (superseded / terminated) versus surfacing a
   * correctable message (invalid / forbidden). Absent in read-only / shared
   * views — the card then shows a waiting state.
   */
  onAnswer?: (
    callId: string,
    callToken: string,
    answers: QuestionAnswerItemPayload[],
    notes?: string,
  ) => Promise<AnswerQuestionResult>;
}

/** Per-question working state: selected option indexes XOR the "Other" text. */
type Draft = { selected: number[]; other: string };

/**
 * What the answer POST reported, held until the authoritative
 * `user_question_answered` event settles the card. `pending: false` means no
 * such event is coming (the session is gone), so the row shows no spinner and
 * the badge must not imply one is still on its way.
 */
type Sent = { badge: string; note: string; pending: boolean };

/** Render the chosen answer for a settled question as display text. */
function renderChosen(q: UserQuestionItem, a: QuestionAnswerItemPayload): string {
  if (typeof a.text === 'string' && a.text.trim()) return a.text.trim();
  const idxs = a.selected_indexes ?? [];
  return idxs.map((i) => q.options[i]?.label ?? `#${i}`).join(', ') || '(no answer)';
}

/**
 * Inline ask-user-question card (native human-in-the-loop clarification),
 * rendered in ConversationTimeline between a user query and the assistant
 * response — the console analog of the plan-approval gate. While the card is
 * answerable it renders each question's options as radios (single-select) or
 * checkboxes (`multi_select`), plus an always-present "Other" free-text input,
 * an optional group-level notes box when the model supplied a placeholder for
 * one, and one Submit button disabled until every question has a selection or
 * text (notes stay optional).
 *
 * **Only `answered` closes the card.** `timed_out`/`declined`/`interrupted`/
 * `cancelled` record that the RUN stopped waiting, which is not the same as the
 * question being resolved — the user who comes back an hour later must still be
 * able to answer, so the form stays live and the card says plainly that the
 * answer will arrive as a new message. Greying it out on those outcomes would
 * hide the affordance exactly when someone finally came to use it.
 *
 * A 404/409 from Submit settles the card as answered-elsewhere with no error
 * residue (the matching `user_question_answered` event flips the status
 * authoritatively).
 */
export function QuestionCard({ question, onAnswer }: QuestionCardProps) {
  const {
    callId,
    callToken,
    questions,
    status,
    answers,
    answeredVia,
    notes,
    notesPlaceholder,
    timeoutSeconds,
    delivery,
  } = question;

  const [drafts, setDrafts] = useState<Draft[]>(() =>
    questions.map(() => ({ selected: [], other: '' })),
  );
  const [notesDraft, setNotesDraft] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [sent, setSent] = useState<Sent | null>(null);
  const [error, setError] = useState<string | null>(null);

  const isAnswered = status === 'answered';
  // The run stopped waiting, but the question itself is still open.
  const runMovedOn = !isAnswered && status !== 'pending';
  // The form is live until the question is genuinely answered — either by the
  // authoritative event or by this card's own accepted POST.
  const answerable = !isAnswered && sent === null;

  const allAnswered = useMemo(
    () => drafts.every((d) => d.other.trim() !== '' || d.selected.length > 0),
    [drafts],
  );

  const setSelected = (qi: number, next: number[]) =>
    setDrafts((prev) =>
      // Selecting an option is XOR with free text — clear "Other" for this row.
      prev.map((d, i) => (i === qi ? { selected: next, other: '' } : d)),
    );

  const setOther = (qi: number, text: string) =>
    setDrafts((prev) =>
      // Typing free text clears any selected indexes (XOR per the contract).
      prev.map((d, i) => (i === qi ? { selected: text ? [] : d.selected, other: text } : d)),
    );

  const toggleCheckbox = (qi: number, oi: number) => {
    const cur = drafts[qi].selected;
    setSelected(qi, cur.includes(oi) ? cur.filter((x) => x !== oi) : [...cur, oi]);
  };

  const submit = async () => {
    if (!onAnswer || submitting || !allAnswered) return;
    const items: QuestionAnswerItemPayload[] = drafts.map((d) =>
      d.other.trim() ? { text: d.other.trim() } : { selected_indexes: d.selected },
    );
    const trimmedNotes = notesDraft.trim();
    setSubmitting(true);
    setError(null);
    try {
      const res = await onAnswer(
        callId,
        callToken,
        items,
        trimmedNotes ? trimmedNotes : undefined,
      );
      if (res.ok) {
        setSent({ badge: 'Sent', note: deliveryNote(res.delivery), pending: true });
      } else if (res.kind === 'superseded') {
        // Resolved elsewhere — settle silently and let the
        // `user_question_answered` event render the authoritative outcome.
        setSent({ badge: 'Resolved', note: 'Already answered elsewhere.', pending: true });
      } else if (res.kind === 'terminated') {
        // No event will ever arrive for a terminated session, so say so rather
        // than parking the card under a spinner that never resolves.
        setSent({
          badge: 'Not delivered',
          note: 'This session was terminated — the answer was not delivered.',
          pending: false,
        });
      } else {
        setError(res.message);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to submit answer.');
    } finally {
      setSubmitting(false);
    }
  };

  const title = questions.length > 1 ? `${questions.length} questions` : 'Question';
  const notesId = `q-notes-${callId}`;

  const { Icon, iconClass, badgeClass, statusLabel } = cardChrome(status, sent);

  return (
    <div className="rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--card))] overflow-hidden">
      <div className="flex items-center gap-3 px-4 py-3">
        <Icon className={`w-4 h-4 shrink-0 ${iconClass}`} />
        <span className="text-sm font-medium text-[hsl(var(--foreground))]">
          {title}
        </span>
        <span
          className={`text-2xs font-medium px-2 py-0.5 rounded-full border ${badgeClass}`}
        >
          {statusLabel}
        </span>
        {answerable && !runMovedOn && typeof timeoutSeconds === 'number' && (
          <span className="ml-auto shrink-0 text-2xs text-[hsl(var(--muted-foreground))]">
            waits up to {formatWait(timeoutSeconds)}
          </span>
        )}
      </div>

      <div className="border-t border-[hsl(var(--border))] px-4 py-3 space-y-4">
        {questions.map((q, qi) => (
          <fieldset key={qi} className="min-w-0">
            <div className="flex items-center gap-2 mb-1.5">
              <span className="text-2xs font-medium px-2 py-0.5 rounded-full border border-[hsl(var(--border))] bg-[hsl(var(--muted))]/40 text-[hsl(var(--muted-foreground))]">
                {q.header}
              </span>
            </div>
            <legend className="text-sm text-[hsl(var(--foreground))] mb-2">
              {q.question}
            </legend>

            {answerable ? (
              <div className="space-y-1.5">
                {q.options.map((opt, oi) => {
                  const checked = drafts[qi]?.selected.includes(oi) ?? false;
                  return (
                    <label
                      key={oi}
                      className="flex items-start gap-2.5 px-2.5 py-2 rounded border border-[hsl(var(--border))] hover:bg-[hsl(var(--accent))]/40 transition-colors cursor-pointer"
                    >
                      <input
                        type={q.multi_select ? 'checkbox' : 'radio'}
                        name={`q-${callId}-${qi}`}
                        checked={checked}
                        disabled={submitting}
                        onChange={() =>
                          q.multi_select
                            ? toggleCheckbox(qi, oi)
                            : setSelected(qi, [oi])
                        }
                        className="mt-0.5 accent-[hsl(var(--primary))]"
                      />
                      <span className="min-w-0">
                        <span className="text-xs font-medium text-[hsl(var(--foreground))]">
                          {opt.label}
                        </span>
                        {opt.description && (
                          <span className="block text-2xs text-[hsl(var(--muted-foreground))] leading-snug">
                            {opt.description}
                          </span>
                        )}
                      </span>
                    </label>
                  );
                })}
                <Input
                  type="text"
                  value={drafts[qi]?.other ?? ''}
                  disabled={submitting}
                  onChange={(e) => setOther(qi, e.target.value)}
                  placeholder={q.options.length ? 'Other (type a different answer)…' : 'Type your answer…'}
                  aria-label={`Free-text answer for ${q.header}`}
                  className="mt-1 bg-[hsl(var(--background))]"
                />
              </div>
            ) : isAnswered ? (
              <SettledAnswer q={q} answer={answers?.[qi]} />
            ) : null}
          </fieldset>
        ))}

        {answerable && notesPlaceholder && (
          <div className="min-w-0">
            <label htmlFor={notesId} className="sr-only">
              Additional notes (optional)
            </label>
            <Textarea
              id={notesId}
              value={notesDraft}
              disabled={submitting}
              maxLength={MAX_NOTES_CHARS}
              onChange={(e) => setNotesDraft(e.target.value)}
              placeholder={notesPlaceholder}
              className="bg-[hsl(var(--background))]"
            />
          </div>
        )}

        {answerable && runMovedOn && (
          <p className="text-2xs text-[hsl(var(--muted-foreground))]">
            {runStoppedNote(status)}
          </p>
        )}

        {answerable && (
          <div className="flex flex-wrap items-center gap-3 pt-1">
            {/* Resting-tinted transcript affordance, matching PlanCard's
                Approve/Reject two cards up. `Button`'s `tone` tints on HOVER
                only, so routing this through it would mean overriding every
                colour leg plus the pill radius — the shared FOCUS_RING is what
                actually matters here, and it is applied. */}
            <button
              type="button"
              onClick={submit}
              disabled={!onAnswer || submitting || !allAnswered}
              className={`inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded border border-[hsl(var(--primary))]/40 bg-[hsl(var(--primary))]/10 text-[hsl(var(--primary-text))] hover:bg-[hsl(var(--primary))]/20 transition-colors disabled:opacity-50 disabled:cursor-not-allowed ${FOCUS_RING}`}
            >
              {submitting && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
              Submit
            </button>
            {!onAnswer && (
              <span className="text-2xs text-[hsl(var(--muted-foreground))]">
                Read-only view.
              </span>
            )}
            {error && (
              <span className="text-2xs text-[hsl(var(--warning))]" role="alert">
                {error}
              </span>
            )}
          </div>
        )}

        {sent && !isAnswered && (
          <div className="flex items-center gap-1.5 pt-1 text-2xs text-[hsl(var(--muted-foreground))]">
            {sent.pending && <Loader2 className="w-3 h-3 animate-spin" />}
            {sent.note}
          </div>
        )}

        {isAnswered && notes && (
          <p className="text-xs text-[hsl(var(--foreground))]">
            <span className="text-[hsl(var(--muted-foreground))]">Notes: </span>
            {notes}
          </p>
        )}

        {isAnswered && (answeredVia || delivery === 'message') && (
          <p className="text-2xs text-[hsl(var(--muted-foreground))]">
            {answeredVia ? `answered via ${answeredVia}` : 'answered'}
            {delivery === 'message' && ' — the run had moved on, so it arrived as a new message'}
          </p>
        )}
      </div>
    </div>
  );
}

/** The chosen answer for a single question on an answered card. */
function SettledAnswer({
  q,
  answer,
}: {
  q: UserQuestionItem;
  answer?: QuestionAnswerItemPayload;
}) {
  if (!answer) {
    return (
      <p className="text-xs text-[hsl(var(--muted-foreground))]">No answer recorded.</p>
    );
  }
  return (
    <p className="text-xs text-[hsl(var(--foreground))]">
      <span className="text-[hsl(var(--muted-foreground))]">Your answer: </span>
      {renderChosen(q, answer)}
    </p>
  );
}

/**
 * Honest copy for a card whose run stopped waiting. It names what happened and
 * what answering now does, because the question is still open. `cancelled` gets
 * its own tail: a cancelled session may refuse the answer outright, so
 * promising it lands as a new message would be a promise we cannot keep.
 */
function runStoppedNote(status: QuestionMeta['status']): string {
  const late = 'You can still answer — it arrives as a new message.';
  switch (status) {
    case 'timed_out':
      return `The run stopped waiting for an answer. ${late}`;
    case 'declined':
      return `The run moved on to a newer message. ${late}`;
    case 'interrupted':
      return `The run was interrupted before an answer arrived. ${late}`;
    default:
      return (
        'The session was cancelled before an answer arrived. You can still ' +
        'answer, though a cancelled session may no longer accept it.'
      );
  }
}

/** Confirm what actually happened to an accepted answer, never a generic "sent". */
function deliveryNote(delivery?: AnswerDelivery): string {
  if (delivery === 'message') {
    return 'The run had already moved on — sent as a new message.';
  }
  if (delivery === 'run') return 'Delivered to the run that was waiting.';
  return 'Answer sent.';
}

/** Understated wait hint — a coarse duration, deliberately not a live
 *  countdown: the server owns expiry, and a ticking clock would imply this
 *  card stops working when it hits zero, which is exactly what it no longer
 *  does. */
function formatWait(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  return `${Math.round(seconds / 3600)}h`;
}

/** Header icon + badge styling for the card's status. */
function cardChrome(status: QuestionMeta['status'], sent: Sent | null) {
  if (status === 'answered') {
    return {
      Icon: CheckCircle2,
      iconClass: 'text-[hsl(var(--success))]',
      badgeClass: 'bg-[hsl(var(--success)/0.1)] text-[hsl(var(--success))] border-[hsl(var(--success)/0.3)]',
      statusLabel: 'Answered',
    };
  }
  if (sent) {
    // Muted until the authoritative event lands, but the label is the POST's
    // own verdict — never a generic "submitting" over a refused answer.
    return {
      Icon: sent.pending ? CheckCircle2 : MinusCircle,
      iconClass: 'text-[hsl(var(--muted-foreground))]',
      badgeClass:
        'bg-[hsl(var(--muted))]/40 text-[hsl(var(--muted-foreground))] border-[hsl(var(--border))]',
      statusLabel: sent.badge,
    };
  }
  if (status === 'pending') {
    return {
      Icon: MessageCircleQuestion,
      iconClass: 'text-[hsl(var(--info))]',
      badgeClass: 'bg-[hsl(var(--info)/0.1)] text-[hsl(var(--info))] border-[hsl(var(--info)/0.3)]',
      statusLabel: 'Awaiting your answer',
    };
  }
  // The run stopped waiting. Muted, but it keeps the question icon and an
  // honest label: the card is still a live form, not a closed record.
  return {
    Icon: MessageCircleQuestion,
    iconClass: 'text-[hsl(var(--muted-foreground))]',
    badgeClass:
      'bg-[hsl(var(--muted))]/40 text-[hsl(var(--muted-foreground))] border-[hsl(var(--border))]',
    statusLabel: 'Still open',
  };
}
