/**
 * PipelineBoard — the Tasks view as an animated pipeline of rings.
 *
 * A prominent bordered rail of four big stage rings — Plan → Code → Review →
 * Done (with factory sub-labels) — sits above four task columns. The active ring
 * glows + animates; stage transitions fly a package-box between rings. Each task
 * card carries its own live state inside it: an animated current-stage icon, a
 * terminal per active agent/subtask (coding), and a Plan·Code·Validate·Log strip
 * with a robot thumbs-up (done) / thumbs-down + red cross (failed).
 */
import { AnimatePresence, motion } from 'motion/react';
import {
  useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState,
  type CSSProperties,
} from 'react';
import type { Task } from '../../shared/types/task';
import { TaskCard } from '../TaskCard';
import {
  CrossIcon, DoneIcon, PackageIcon, PlanDocIcon, ReviewIcon,
  RobotHeadIcon, RobotThumbsDownIcon, RobotThumbsUpIcon, SignalIcon, TerminalIcon,
} from './icons';
import './pipeline.css';

type Stage = 'plan' | 'code' | 'review' | 'done';

const PLAN_PHASES = new Set(['spec_creation', 'planning', 'plan_review']);
const REVIEW_PHASES = new Set(['qa_review', 'qa_fixing']);
const ORDER: Stage[] = ['plan', 'code', 'review', 'done'];
const MAX_TERMS = 5;

const STAGES: { key: Stage; label: string; sub: string; Icon: typeof RobotHeadIcon }[] = [
  { key: 'plan', label: 'Plan', sub: 'PFactory', Icon: PlanDocIcon },
  { key: 'code', label: 'Code', sub: 'AIFactory', Icon: RobotHeadIcon },
  { key: 'review', label: 'Review', sub: 'TFactory', Icon: ReviewIcon },
  { key: 'done', label: 'Done', sub: 'Shipped', Icon: DoneIcon },
];

function stageOf(task: Task): Stage {
  const phase = task.executionProgress?.phase;
  if (task.status === 'done') return 'done';
  if (phase === 'coding') return 'code';
  if (phase && REVIEW_PHASES.has(phase)) return 'review';
  if (phase && PLAN_PHASES.has(phase)) return 'plan';
  if (task.status === 'in_progress') return 'code';
  if (task.status === 'ai_review' || task.status === 'human_review') return 'review';
  if (task.status === 'backlog') return 'plan';
  return 'plan';
}

function isCompleted(task: Task): boolean {
  if (task.status === 'done') return true;
  if (task.reviewReason === 'completed') return true;
  return task.executionProgress?.phase === 'complete';
}

function isFailed(task: Task): boolean {
  return task.reviewReason === 'errors'
    || task.reviewReason === 'qa_rejected'
    || task.executionProgress?.phase === 'failed';
}

/** Active agents (terminals) for a coding task — its in-flight subtasks. */
function agentCount(task: Task): number {
  const phase = task.executionProgress?.phase;
  const coding = phase === 'coding' || (task.status === 'in_progress' && stageOf(task) === 'code');
  if (!coding) return 0;
  return Math.max((task.subtasks || []).filter((s) => s.status === 'in_progress').length, 1);
}

/** The stage a task is actively working RIGHT NOW (null when idle/terminal). */
function activeStage(task: Task): Stage | null {
  const phase = task.executionProgress?.phase;
  if (!phase || phase === 'idle' || phase === 'complete' || phase === 'failed') return null;
  if (phase === 'coding') return 'code';
  if (REVIEW_PHASES.has(phase)) return 'review';
  if (PLAN_PHASES.has(phase)) return 'plan';
  return null;
}

type PState = 'done' | 'active' | 'failed' | 'pending';

function phaseStrip(task: Task): { key: string; label: string; Icon: typeof RobotHeadIcon; state: PState }[] {
  const phase = task.executionProgress?.phase;
  const done = isCompleted(task);
  const failed = isFailed(task);
  const reached = (...stages: Stage[]) => stages.includes(stageOf(task));

  const plan: PState = done || reached('code', 'review', 'done') ? 'done'
    : phase && PLAN_PHASES.has(phase) ? 'active'
    : task.status === 'backlog' && !phase ? 'pending' : 'done';
  const code: PState = done || reached('review', 'done') ? 'done'
    : phase === 'coding' || reached('code') ? 'active'
    : failed && task.reviewReason === 'errors' ? 'failed' : 'pending';
  const validation: PState = done ? 'done'
    : task.reviewReason === 'qa_rejected' ? 'failed'
    : phase && REVIEW_PHASES.has(phase) ? 'active'
    : failed ? 'failed' : 'pending';
  const log: PState = (task.logs && task.logs.length > 0) || phase ? 'done' : 'pending';

  return [
    { key: 'plan', label: 'Plan', Icon: PlanDocIcon, state: plan },
    { key: 'code', label: 'Code', Icon: RobotHeadIcon, state: code },
    { key: 'val', label: 'Validate', Icon: ReviewIcon, state: validation },
    { key: 'log', label: 'Log', Icon: TerminalIcon, state: log },
  ];
}

const STAGE_ICON: Record<Stage, typeof RobotHeadIcon> = {
  plan: PlanDocIcon, code: RobotHeadIcon, review: ReviewIcon, done: DoneIcon,
};

interface Flight { id: string; to: number; x0: number; x1: number; y: number; }

interface Props {
  tasks: Task[];
  onTaskClick: (task: Task) => void;
  onNewTaskClick: () => void;
  isInitialized?: boolean;
}

export function PipelineBoard({ tasks, onTaskClick, onNewTaskClick }: Props) {
  const boardRef = useRef<HTMLDivElement>(null);
  const ringRefs = useRef<(HTMLDivElement | null)[]>([]);
  const [centers, setCenters] = useState<{ x: number; y: number }[]>([]);
  const [flights, setFlights] = useState<Flight[]>([]);
  const prevStage = useRef<Map<string, Stage>>(new Map());

  const byStage = useMemo(() => {
    const g: Record<Stage, Task[]> = { plan: [], code: [], review: [], done: [] };
    for (const t of tasks) g[stageOf(t)].push(t);
    for (const k of ORDER) g[k].sort((a, b) => new Date(b.createdAt).getTime() - new Date(a.createdAt).getTime());
    return g;
  }, [tasks]);

  const active = useMemo(() => {
    const a: Record<Stage, boolean> = { plan: false, code: false, review: false, done: false };
    for (const t of tasks) {
      const s = activeStage(t);
      if (s) a[s] = true;
    }
    return a;
  }, [tasks]);

  const measure = useCallback(() => {
    const board = boardRef.current;
    if (!board) return;
    const br = board.getBoundingClientRect();
    setCenters(ringRefs.current.map((el) => {
      if (!el) return { x: 0, y: 0 };
      const r = el.getBoundingClientRect();
      return { x: r.left - br.left + r.width / 2, y: r.top - br.top + r.height / 2 };
    }));
  }, []);

  useLayoutEffect(() => { measure(); }, [measure, tasks.length]);
  useEffect(() => {
    const board = boardRef.current;
    if (!board) return;
    const ro = new ResizeObserver(() => { measure(); });
    ro.observe(board);
    window.addEventListener('resize', measure);
    return () => { ro.disconnect(); window.removeEventListener('resize', measure); };
  }, [measure]);

  useEffect(() => {
    if (centers.length < ORDER.length) {
      if (prevStage.current.size === 0) for (const t of tasks) prevStage.current.set(t.id, stageOf(t));
      return;
    }
    const launched: Flight[] = [];
    const seen = new Set<string>();
    for (const t of tasks) {
      seen.add(t.id);
      const cur = stageOf(t);
      const was = prevStage.current.get(t.id);
      prevStage.current.set(t.id, cur);
      if (was && was !== cur) {
        const from = ORDER.indexOf(was), to = ORDER.indexOf(cur);
        launched.push({ id: `${t.id}-${was}-${cur}-${Date.now()}`, to, x0: centers[from].x, x1: centers[to].x, y: centers[from].y });
      }
    }
    for (const id of [...prevStage.current.keys()]) if (!seen.has(id)) prevStage.current.delete(id);
    if (launched.length) setFlights((f) => [...f, ...launched]);
  }, [tasks, centers]);

  const removeFlight = useCallback((id: string) => { setFlights((f) => f.filter((x) => x.id !== id)); }, []);

  return (
    <div ref={boardRef} className="pl-board">
      {/* prominent ring rail */}
      <div className="pl-railpanel">
        <div className="pl-rail">
          {STAGES.map((stage, i) => {
            const isActive = active[stage.key];
            const cssVar = { ['--c' as string]: `var(--pl-${stage.key})` } as CSSProperties;
            return (
              <div className="pl-rail-cell" key={stage.key} style={cssVar}>
                <div className={`pl-stage ${isActive ? 'is-active' : byStage[stage.key].length === 0 ? 'is-idle' : ''}`}>
                  <div className="pl-ring" ref={(el) => { ringRefs.current[i] = el; }}>
                    <stage.Icon size={40} />
                    <span className="pl-badge">{byStage[stage.key].length}</span>
                    {stage.key === 'plan' && <span className="pl-mcp" title="Fetching context (MCP)"><SignalIcon /></span>}
                  </div>
                  <span className="pl-label">{stage.label}</span>
                  <span className="pl-sublabel">{stage.sub}</span>
                </div>
              </div>
            );
          })}
        </div>
        <div className="pl-connectors" aria-hidden>
          {centers.length === ORDER.length && ORDER.slice(0, -1).map((s, i) => {
            const a = centers[i], b = centers[i + 1];
            const flowing = active[ORDER[i]] || active[ORDER[i + 1]];
            return (
              <div key={s} className={`pl-conn ${flowing ? 'is-flowing' : ''}`}
                style={{ left: a.x + 56, top: a.y - 1.5, width: Math.max(b.x - a.x - 112, 0),
                         ['--c' as string]: `var(--pl-${s})` }} />
            );
          })}
        </div>
      </div>

      {/* task columns */}
      <div className="pl-board-grid">
        {STAGES.map((stage) => {
          const list = byStage[stage.key];
          const cssVar = { ['--c' as string]: `var(--pl-${stage.key})` } as CSSProperties;
          return (
            <div className="pl-col" key={stage.key} style={cssVar}>
              <div className="pl-col-list">
                {stage.key === 'plan' && <button className="pl-add" onClick={onNewTaskClick}>+ New task</button>}
                <AnimatePresence initial={false}>
                  {list.map((task) => {
                    const done = isCompleted(task);
                    const failed = !done && isFailed(task);
                    const now = activeStage(task);
                    const NowIcon = now ? STAGE_ICON[now] : null;
                    const agents = Math.min(agentCount(task), MAX_TERMS);
                    const overflow = agentCount(task) - agents;
                    return (
                      <motion.div key={task.id} layout
                        className={`pl-card-wrap ${failed ? 'is-failed' : ''} ${done ? 'is-done' : ''} ${now ? 'is-live' : ''}`}
                        initial={{ opacity: 0, y: 10, scale: 0.98 }}
                        animate={{ opacity: 1, y: 0, scale: 1 }}
                        exit={{ opacity: 0, scale: 0.92 }}
                        transition={{ type: 'spring', stiffness: 380, damping: 30 }}>
                        {done && (
                          <motion.span className="pl-card-done" aria-label="Task done"
                            initial={{ scale: 0, rotate: -25 }} animate={{ scale: 1, rotate: 0 }}
                            transition={{ type: 'spring', stiffness: 500, damping: 14, delay: 0.1 }}>
                            <RobotThumbsUpIcon size={20} />
                          </motion.span>
                        )}
                        {failed && (
                          <motion.span className="pl-card-fail" aria-label="Task failed"
                            initial={{ scale: 0, rotate: 25 }} animate={{ scale: 1, rotate: 0 }}
                            transition={{ type: 'spring', stiffness: 500, damping: 14, delay: 0.1 }}>
                            <RobotThumbsDownIcon size={20} />
                          </motion.span>
                        )}
                        {!done && !failed && NowIcon && (
                          <span className="pl-card-now" title={`Now: ${now}`}><NowIcon size={18} /></span>
                        )}
                        {failed && <span className="pl-card-cross" aria-hidden><CrossIcon size={84} /></span>}

                        <TaskCard task={task} onClick={() => { onTaskClick(task); }} />

                        {agents > 0 && (
                          <div className="pl-card-agents" title={`${agentCount(task)} agent(s) active`}>
                            <span className="pl-card-agents-lbl">agents</span>
                            <AnimatePresence>
                              {Array.from({ length: agents }).map((_, idx) => (
                                <motion.span key={idx} className="pl-term"
                                  initial={{ opacity: 0, scale: 0.4 }} animate={{ opacity: 1, scale: 1 }}
                                  exit={{ opacity: 0, scale: 0.4 }}
                                  transition={{ delay: idx * 0.07, type: 'spring', stiffness: 420, damping: 26 }}>
                                  <TerminalIcon size={18} />
                                </motion.span>
                              ))}
                            </AnimatePresence>
                            {overflow > 0 && <span className="pl-term-more">+{overflow}</span>}
                          </div>
                        )}

                        <div className="pl-phase-strip" aria-hidden>
                          {phaseStrip(task).map((p) => (
                            <span key={p.key} className="pl-chip" data-state={p.state} title={`${p.label}: ${p.state}`}>
                              <span className="pl-chip-ico">
                                <p.Icon size={14} />
                                {p.state === 'failed' && <span className="pl-chip-x"><CrossIcon size={11} /></span>}
                              </span>
                              <span className="pl-chip-lbl">{p.label}</span>
                            </span>
                          ))}
                        </div>
                      </motion.div>
                    );
                  })}
                </AnimatePresence>
                {list.length === 0 && stage.key !== 'plan' && <div className="pl-empty">{emptyText(stage.key)}</div>}
              </div>
            </div>
          );
        })}
      </div>

      <div className="pl-flights" aria-hidden>
        <AnimatePresence>
          {flights.map((fl) => (
            <motion.div key={fl.id} className="pl-pkg"
              style={{ top: fl.y, ['--c' as string]: `var(--pl-${ORDER[fl.to]})` }}
              initial={{ left: fl.x0, opacity: 0, scale: 0.5 }}
              animate={{ left: fl.x1, opacity: [0, 1, 1, 0], scale: [0.5, 1.15, 1, 0.8], y: [0, -26, -26, 0] }}
              transition={{ duration: 1.1, ease: 'easeInOut', times: [0, 0.2, 0.8, 1] }}
              onAnimationComplete={() => { removeFlight(fl.id); }}>
              <PackageIcon size={26} />
            </motion.div>
          ))}
        </AnimatePresence>
      </div>
    </div>
  );
}

function emptyText(stage: Stage): string {
  switch (stage) {
    case 'code': return 'Nothing building';
    case 'review': return 'Nothing in review';
    case 'done': return 'No completed tasks yet';
    default: return '';
  }
}
