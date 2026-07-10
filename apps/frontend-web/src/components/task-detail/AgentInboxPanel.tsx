/**
 * AgentInboxPanel — Send messages into a running task's agent inbox.
 *
 * Consumes:
 *   POST /api/tasks/{id}/inbox   body: { text: string }
 *     → { messageId: string; delivered: boolean }
 *   GET  /api/tasks/{id}/inbox
 *     → { messages: Array<{ id: string; text: string; createdAt: string; delivered: boolean }> }
 *
 * Supports @agent / #task mention hint text (backend parses them).
 */

import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Send, CheckCircle2, MessageSquare } from 'lucide-react';
import { get, post } from '../../lib/api-client';
import { Button } from '../ui/button';
import { Textarea } from '../ui/textarea';
import { Badge } from '../ui/badge';
import { cn } from '../../lib/utils';

interface InboxMessage {
  id: string;
  text: string;
  createdAt: string;
  delivered: boolean;
}

interface AgentInboxPanelProps {
  taskId: string;
}

function formatTime(isoString: string): string {
  try {
    return new Date(isoString).toLocaleTimeString([], {
      hour: '2-digit',
      minute: '2-digit',
    });
  } catch {
    return '';
  }
}

export function AgentInboxPanel({ taskId }: AgentInboxPanelProps) {
  const { t } = useTranslation(['tasks']);
  const [messages, setMessages] = useState<InboxMessage[]>([]);
  const [text, setText] = useState('');
  const [sending, setSending] = useState(false);
  const [lastDelivered, setLastDelivered] = useState<boolean | null>(null);
  const listEndRef = useRef<HTMLDivElement>(null);

  // Load existing messages once on mount
  useEffect(() => {
    let cancelled = false;
    get<{ messages: InboxMessage[] }>(`/tasks/${encodeURIComponent(taskId)}/inbox`)
      .then((result) => {
        if (cancelled) return;
        if (result.success && result.data?.messages) {
          setMessages(result.data.messages);
        }
      })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [taskId]);

  // Scroll to bottom when messages change
  useEffect(() => {
    listEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  const handleSend = async () => {
    const trimmed = text.trim();
    if (!trimmed || sending) return;

    setSending(true);
    setLastDelivered(null);

    const result = await post<{ messageId: string; delivered: boolean }>(
      `/tasks/${encodeURIComponent(taskId)}/inbox`,
      { text: trimmed }
    );

    if (result.success && result.data) {
      const newMsg: InboxMessage = {
        id: result.data.messageId,
        text: trimmed,
        createdAt: new Date().toISOString(),
        delivered: result.data.delivered,
      };
      setMessages((prev) => [...prev, newMsg]);
      setLastDelivered(result.data.delivered);
      setText('');
    }
    setSending(false);
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      void handleSend();
    }
  };

  return (
    <div className="flex flex-col gap-3">
      {/* Message list */}
      {messages.length > 0 ? (
        <div className="space-y-2 max-h-48 overflow-y-auto pr-1">
          {messages.map((msg) => (
            <div
              key={msg.id}
              className="rounded-lg bg-muted px-3 py-2 text-sm space-y-1"
            >
              <div className="flex items-start justify-between gap-2">
                <p className="text-foreground leading-snug break-words flex-1">
                  {msg.text}
                </p>
                {msg.delivered ? (
                  <CheckCircle2 className="h-3.5 w-3.5 text-success shrink-0 mt-0.5" />
                ) : (
                  <span className="text-xs text-muted-foreground shrink-0 mt-0.5">
                    {t('tasks:agentInbox.queued')}
                  </span>
                )}
              </div>
              <span className="text-xs text-muted-foreground/70">
                {formatTime(msg.createdAt)}
              </span>
            </div>
          ))}
          <div ref={listEndRef} />
        </div>
      ) : (
        <div className="flex items-center gap-2 py-3 text-sm text-muted-foreground">
          <MessageSquare className="h-4 w-4 text-muted-foreground/40" />
          <span>{t('tasks:agentInbox.empty')}</span>
        </div>
      )}

      {/* Compose area */}
      <div className="space-y-2">
        <Textarea
          value={text}
          onChange={(e) => { setText(e.target.value); }}
          onKeyDown={handleKeyDown}
          placeholder={t('tasks:agentInbox.placeholder')}
          rows={2}
          className="resize-none text-sm"
          disabled={sending}
          aria-label={t('tasks:agentInbox.inputLabel')}
        />
        <div className="flex items-center justify-between gap-2">
          <p className="text-xs text-muted-foreground/60">
            {t('tasks:agentInbox.mentionHint')}
          </p>
          <div className="flex items-center gap-2 shrink-0">
            {lastDelivered !== null && (
              <Badge
                variant={lastDelivered ? 'success' : 'secondary'}
                className={cn('text-xs', lastDelivered && 'text-success')}
              >
                {lastDelivered
                  ? t('tasks:agentInbox.delivered')
                  : t('tasks:agentInbox.queued')}
              </Badge>
            )}
            <Button
              size="sm"
              onClick={handleSend}
              disabled={!text.trim() || sending}
              aria-label={t('tasks:agentInbox.sendButton')}
            >
              <Send className="h-3.5 w-3.5 mr-1.5" />
              {sending
                ? t('tasks:agentInbox.sending')
                : t('tasks:agentInbox.sendButton')}
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}
