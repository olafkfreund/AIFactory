import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Button } from '../ui/button';
import { answerQuestion, decideQuestion, getQuestions, QuestionRequestError, type HumanQuestion } from '../../lib/questions-api';

export function NeedsInputBanner({ taskId, rejected = false }: { taskId: string; rejected?: boolean }) {
  const { t } = useTranslation('tasks');
  const [questions, setQuestions] = useState<HumanQuestion[]>([]);
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState('');

  async function refresh() {
    const result = await getQuestions(taskId);
    if (result.success) setQuestions(result.data?.questions ?? []);
  }

  useEffect(() => {
    let active = true;
    getQuestions(taskId).then((result) => {
      if (!active) return;
      if (result.success) setQuestions(result.data?.questions ?? []);
      else setError(t('needsInput.loadError'));
    });
    return () => { active = false; };
  }, [taskId, t]);

  async function submit(q: HumanQuestion, value: string, decision?: 'approve' | 'reject') {
    setBusy(q.id);
    setError('');
    try {
      if (decision) await decideQuestion(taskId, q.id, decision, value);
      else await answerQuestion(taskId, q.id, value);
      setQuestions((current) => current.filter((item) => item.id !== q.id));
    } catch (cause) {
      const status = cause instanceof QuestionRequestError ? cause.status : 0;
      const key = status === 409 ? 'alreadyAnswered'
        : status === 403 ? decision ? 'approvalSignIn' : 'forbidden'
        : status === 404 ? 'notFound'
        : status === 422 ? 'invalidAnswer'
        : cause instanceof Error && cause.message === 'confirm' ? 'confirmError' : 'answerError';
      setError(t(`needsInput.${key}`));
      if (status === 409) await refresh();
    } finally {
      setBusy(null);
    }
  }

  return <section role="region" aria-label={t('needsInput.title')} className="rounded-lg border border-warning/40 bg-warning/10 p-4 space-y-3">
    <h3 className="font-semibold">{t(rejected ? 'needsInput.rejectedTitle' : 'needsInput.title')}</h3>
    {rejected && <p className="text-sm">{t('needsInput.rejectedNotice')}</p>}
    {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
    {questions.filter((q) => rejected ? q.status === 'rejected' : q.status === 'open' || q.status === 'expired').map((q) => <div key={q.id} className="space-y-2">
      <p className="text-sm">{q.question}</p>
      {q.context && <p className="text-xs text-muted-foreground">{q.context}</p>}
      {rejected ? q.note && <p className="text-sm">{t('needsInput.rejectedNote', { note: q.note })}</p> : <>
      {q.default && <p className="text-xs text-muted-foreground">{t('needsInput.suggested', { suggestion: q.default })}</p>}
      {q.needs_approval ? <div className="space-y-2">
        <input aria-label={t('needsInput.note')} className="w-full rounded border border-input bg-background px-3 py-2 text-sm" value={answers[q.id] ?? ''} onChange={(event) => setAnswers({ ...answers, [q.id]: event.target.value })} />
        <div className="flex gap-2">
        <Button disabled={busy !== null} onClick={() => void submit(q, answers[q.id] ?? '', 'approve')}>{t('needsInput.approve')}</Button>
        <Button variant="destructive" disabled={busy !== null} onClick={() => void submit(q, answers[q.id] ?? '', 'reject')}>{t('needsInput.reject')}</Button>
        </div>
      </div> : q.choices?.length ? <div className="flex flex-wrap gap-2">
        {q.choices.map((choice) => <Button key={choice} variant="outline" disabled={busy !== null} onClick={() => void submit(q, choice)}>{choice}</Button>)}
      </div> : <form onSubmit={(event) => { event.preventDefault(); void submit(q, (answers[q.id] ?? '').trim()); }} className="flex gap-2">
        <input aria-label={t('needsInput.answer')} className="min-w-0 flex-1 rounded border border-input bg-background px-3 py-2 text-sm" value={answers[q.id] ?? ''} onChange={(event) => setAnswers({ ...answers, [q.id]: event.target.value })} />
        <Button type="submit" disabled={busy !== null || !answers[q.id]?.trim()}>{t('needsInput.send')}</Button>
      </form>}</>}
    </div>)}
  </section>;
}
