/**
 * PipelineBoard — the Tasks view as an animated pipeline of rings.
 *
 * Replaces the old Kanban columns. Four glowing stage rings — Plan → Code →
 * Review → Done — each acting as a column header with its tasks listed beneath.
 * The active stage animates (robot bob/blink, doc scan-line + MCP ping, magnifier,
 * check); the Code ring spawns one terminal per active agent/subtask; and when a
 * task changes stage a package-box icon flies from the old ring to the new one.
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
const MAX_TERMS = 6;

const STAGES: { key: Stage; label: string; Icon: typeof RobotHeadIcon }[] = [
  { key: 'plan', label: 'Plan', Icon: PlanDocIcon },
  { key: 'code', label: 'Code', Icon: RobotHeadIcon },
  { key: 'review', label: 'Review', Icon: ReviewIcon },
  { key: 'done', label: 'Done', Icon: DoneIcon },
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

interface Flight { id: string; from: number; to: number; x0: number; x1: number; y: number; }

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

  // group tasks by stage
  const byStage = useMemo(() => {
    const g: Record<Stage, Task[]> = { plan: [], code: [], review: [], done: [] };
    for (const t of tasks) g[stageOf(t)].push(t);
    for (const k of ORDER) {
      g[k].sort((a, b) => new Date(b.createdAt).getTime() - new Date(a.createdAt).getTime());
    }
    return g;
  }, [tasks]);

  // active flags + agent terminals (active coders)
  const meta = useMemo(() => {
    const active: Record<Stage, boolean> = { plan: false, code: false, review: false, done: false };
    let agents = 0;
    for (const t of tasks) {
      const phase = t.executionProgress?.phase;
      if (phase && PLAN_PHASES.has(phase)) active.plan = true;
      if (phase && REVIEW_PHASES.has(phase)) active.review = true;
      const coding = phase === 'coding' || (t.status === 'in_progress' && stageOf(t) === 'code');
      if (coding) {
        active.code = true;
        agents += Math.max((t.subtasks || []).filter((s) => s.status === 'in_progress').length, 1);
      }
    }
    return { active, agents };
  }, [tasks]);

  // measure ring centers relative to the board (for connectors + flights)
  const measure = useCallback(() => {
    const board = boardRef.current;
    if (!board) return;
    const br = board.getBoundingClientRect();
    const next = ringRefs.current.map((el) => {
      if (!el) return { x: 0, y: 0 };
      const r = el.getBoundingClientRect();
      return { x: r.left - br.left + r.width / 2, y: r.top - br.top + r.height / 2 };
    });
    setCenters(next);
  }, []);

  useLayoutEffect(() => { measure(); }, [measure, tasks.length]);
  useEffect(() => {
    const board = boardRef.current;
    if (!board) return;
    const ro = new ResizeObserver(() => measure());
    ro.observe(board);
    window.addEventListener('resize', measure);
    return () => { ro.disconnect(); window.removeEventListener('resize', measure); };
  }, [measure]);

  // detect stage transitions → launch a package flight between ring centers
  useEffect(() => {
    if (centers.length < ORDER.length) {
      // still seed the map so the first paint doesn't fire spurious flights
      if (prevStage.current.size === 0) {
        for (const t of tasks) prevStage.current.set(t.id, stageOf(t));
      }
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
        const from = ORDER.indexOf(was);
        const to = ORDER.indexOf(cur);
        launched.push({
          id: `${t.id}-${was}-${cur}-${Date.now()}`,
          from, to,
          x0: centers[from].x, x1: centers[to].x, y: centers[from].y,
        });
      }
    }
    for (const id of [...prevStage.current.keys()]) if (!seen.has(id)) prevStage.current.delete(id);
    if (launched.length) setFlights((f) => [...f, ...launched]);
  }, [tasks, centers]);

  const removeFlight = useCallback((id: string) => {
    setFlights((f) => f.filter((x) => x.id !== id));
  }, []);

  return (
    <div ref={boardRef} className="pl-board">
      {/* connectors between ring centers */}
      <div className="pl-connectors" aria-hidden>
        {centers.length === ORDER.length && ORDER.slice(0, -1).map((s, i) => {
          const a = centers[i], b = centers[i + 1];
          const flowing = meta.active[ORDER[i]] || meta.active[ORDER[i + 1]];
          return (
            <div
              key={s}
              className={`pl-conn ${flowing ? 'is-flowing' : ''}`}
              style={{ left: a.x + 40, top: a.y - 1, width: Math.max(b.x - a.x - 80, 0),
                       ['--c' as string]: `var(--pl-${s})` }}
            />
          );
        })}
      </div>

      {/* the four ring columns */}
      <div className="pl-board-grid">
        {STAGES.map((stage, i) => {
          const list = byStage[stage.key];
          const isActive = meta.active[stage.key];
          const cssVar = { ['--c' as string]: `var(--pl-${stage.key})` } as CSSProperties;
          const termCount = stage.key === 'code' ? Math.min(meta.agents, MAX_TERMS) : 0;
          const overflow = stage.key === 'code' ? meta.agents - termCount : 0;
          return (
            <div className={`pl-col ${stage.key === 'code' && isActive ? 'is-coding' : ''}`} key={stage.key} style={cssVar}>
              <div className="pl-col-head">
                <div className={`pl-stage ${isActive ? 'is-active' : list.length === 0 ? 'is-idle' : ''}`}>
                  <div className="pl-ring" ref={(el) => { ringRefs.current[i] = el; }}>
                    <stage.Icon />
                    <span className="pl-badge">{list.length}</span>
                    {stage.key === 'plan' && (
                      <span className="pl-mcp" title="Fetching context (MCP)"><SignalIcon /></span>
                    )}
                  </div>
                  <span className="pl-label">{stage.label}</span>
                  {stage.key === 'code' && (termCount > 0 || overflow > 0) && (
                    <div className="pl-agents">
                      <AnimatePresence>
                        {Array.from({ length: termCount }).map((_, idx) => (
                          <motion.span key={idx} className="pl-term"
                            initial={{ opacity: 0, scale: 0.4, y: -6 }}
                            animate={{ opacity: 1, scale: 1, y: 0 }}
                            exit={{ opacity: 0, scale: 0.4, y: 6 }}
                            transition={{ delay: idx * 0.07, type: 'spring', stiffness: 420, damping: 26 }}>
                            <TerminalIcon />
                          </motion.span>
                        ))}
                      </AnimatePresence>
                      {overflow > 0 && <span className="pl-term-more">+{overflow}</span>}
                    </div>
                  )}
                </div>
              </div>

              <div className="pl-col-list">
                {stage.key === 'plan' && (
                  <button className="pl-add" onClick={onNewTaskClick}>+ New task</button>
                )}
                <AnimatePresence initial={false}>
                  {list.map((task) => {
                    const done = isCompleted(task);
                    const failed = !done && isFailed(task);
                    return (
                    <motion.div key={task.id} layout
                      className={`pl-card-wrap ${failed ? 'is-failed' : ''} ${done ? 'is-done' : ''}`}
                      initial={{ opacity: 0, y: 10, scale: 0.98 }}
                      animate={{ opacity: 1, y: 0, scale: 1 }}
                      exit={{ opacity: 0, scale: 0.92 }}
                      transition={{ type: 'spring', stiffness: 380, damping: 30 }}>
                      {done && (
                        <motion.span className="pl-card-done" aria-label="Task done"
                          initial={{ scale: 0, rotate: -25 }} animate={{ scale: 1, rotate: 0 }}
                          transition={{ type: 'spring', stiffness: 500, damping: 14, delay: 0.1 }}>
                          <RobotThumbsUpIcon size={18} />
                        </motion.span>
                      )}
                      {failed && (
                        <motion.span className="pl-card-fail" aria-label="Task failed"
                          initial={{ scale: 0, rotate: 25 }} animate={{ scale: 1, rotate: 0 }}
                          transition={{ type: 'spring', stiffness: 500, damping: 14, delay: 0.1 }}>
                          <RobotThumbsDownIcon size={18} />
                        </motion.span>
                      )}
                      {!done && !failed && (
                        <span className="pl-card-tick" aria-hidden><stage.Icon size={14} /></span>
                      )}
                      {failed && (
                        <span className="pl-card-cross" aria-hidden><CrossIcon size={84} /></span>
                      )}
                      <TaskCard task={task} onClick={() => onTaskClick(task)} />
                      <div className="pl-phase-strip" aria-hidden>
                        {phaseStrip(task).map((p) => (
                          <span key={p.key} className="pl-chip" data-state={p.state} title={`${p.label}: ${p.state}`}>
                            <span className="pl-chip-ico">
                              <p.Icon size={13} />
                              {p.state === 'failed' && <span className="pl-chip-x"><CrossIcon size={11} /></span>}
                            </span>
                            <span className="pl-chip-lbl">{p.label}</span>
                          </span>
                        ))}
                      </div>
                    </motion.div>
                  );})}
                </AnimatePresence>
                {list.length === 0 && stage.key !== 'plan' && (
                  <div className="pl-empty">{emptyText(stage.key)}</div>
                )}
              </div>
            </div>
          );
        })}
      </div>

      {/* package-box flights on stage transitions */}
      <div className="pl-flights" aria-hidden>
        <AnimatePresence>
          {flights.map((fl) => (
            <motion.div key={fl.id} className="pl-pkg"
              style={{ top: fl.y, ['--c' as string]: `var(--pl-${ORDER[fl.to]})` }}
              initial={{ left: fl.x0, opacity: 0, scale: 0.5 }}
              animate={{ left: fl.x1, opacity: [0, 1, 1, 0], scale: [0.5, 1.1, 1, 0.8],
                         y: [0, -22, -22, 0] }}
              transition={{ duration: 1.1, ease: 'easeInOut', times: [0, 0.2, 0.8, 1] }}
              onAnimationComplete={() => removeFlight(fl.id)}>
              <PackageIcon />
            </motion.div>
          ))}
        </AnimatePresence>
      </div>
    </div>
  );
}

/** A task that finished successfully — gets the robot thumbs-up. */
function isCompleted(task: Task): boolean {
  if (task.status === 'done') return true;
  if (task.reviewReason === 'completed') return true;
  return task.executionProgress?.phase === 'complete';
}

/** A task that failed — gets the robot thumbs-down + a red cross. */
function isFailed(task: Task): boolean {
  return task.reviewReason === 'errors'
    || task.reviewReason === 'qa_rejected'
    || task.executionProgress?.phase === 'failed';
}

type PState = 'done' | 'active' | 'failed' | 'pending';

/** Per-phase status chips for a card: Plan · Code · Validation · Build log. */
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

function emptyText(stage: Stage): string {
  switch (stage) {
    case 'code': return 'Nothing building';
    case 'review': return 'Nothing in review';
    case 'done': return 'No completed tasks yet';
    default: return '';
  }
}
