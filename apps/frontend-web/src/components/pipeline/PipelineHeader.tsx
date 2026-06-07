/**
 * PipelineHeader — an animated Plan → Code → Test census across all tasks.
 *
 * Three glowing stage nodes (Plan = document, Code = robot head, Test = flask)
 * joined by marching-ant connectors, each with a live count badge. The active
 * stage animates: planning shows a doc scan-line + an MCP "fetch" signal ping;
 * coding bobs/blinks the robot and spawns one terminal icon per active
 * agent/subtask; testing bubbles the flask. All derived from live task state.
 */
import { AnimatePresence, motion } from 'motion/react';
import { useMemo, type CSSProperties } from 'react';
import type { Task } from '../../shared/types/task';
import { FlaskIcon, PlanDocIcon, RobotHeadIcon, SignalIcon, TerminalIcon } from './icons';
import './pipeline.css';

type Stage = 'plan' | 'code' | 'test';

const PLAN_PHASES = new Set(['spec_creation', 'planning', 'plan_review']);
const TEST_PHASES = new Set(['qa_review', 'qa_fixing']);
const MAX_TERMS = 6;

function macroStage(task: Task): Stage | null {
  if (task.status === 'done') return null;
  const phase = task.executionProgress?.phase;
  if (phase === 'coding') return 'code';
  if (phase && TEST_PHASES.has(phase)) return 'test';
  if (phase && PLAN_PHASES.has(phase)) return 'plan';
  // fall back to the board column
  if (task.status === 'in_progress') return 'code';
  if (task.status === 'ai_review' || task.status === 'human_review') return 'test';
  if (task.status === 'backlog') return 'plan';
  return null;
}

interface Derived {
  counts: Record<Stage, number>;
  active: Record<Stage, boolean>;
  agents: number; // active coders (one terminal each)
}

function derive(tasks: Task[]): Derived {
  const counts: Record<Stage, number> = { plan: 0, code: 0, test: 0 };
  const active: Record<Stage, boolean> = { plan: false, code: false, test: false };
  let agents = 0;

  for (const task of tasks) {
    const stage = macroStage(task);
    if (stage) counts[stage] += 1;

    const phase = task.executionProgress?.phase;
    if (phase && PLAN_PHASES.has(phase)) active.plan = true;
    if (phase && TEST_PHASES.has(phase)) active.test = true;

    const coding = phase === 'coding' || (task.status === 'in_progress' && stage === 'code');
    if (coding) {
      active.code = true;
      const running = (task.subtasks || []).filter((s) => s.status === 'in_progress').length;
      agents += Math.max(running, 1); // at least the lead agent
    }
  }
  return { counts, active, agents };
}

const STAGES: { key: Stage; label: string; Icon: typeof RobotHeadIcon }[] = [
  { key: 'plan', label: 'Plan', Icon: PlanDocIcon },
  { key: 'code', label: 'Code', Icon: RobotHeadIcon },
  { key: 'test', label: 'Test', Icon: FlaskIcon },
];

export function PipelineHeader({ tasks }: { tasks: Task[] }) {
  const { counts, active, agents } = useMemo(() => derive(tasks), [tasks]);
  const termCount = Math.min(agents, MAX_TERMS);
  const overflow = agents - termCount;

  return (
    <div className="pl-pipeline" role="img"
      aria-label={`Pipeline: ${counts.plan} planning, ${counts.code} coding with ${agents} active agents, ${counts.test} testing`}>
      {STAGES.map((stage, i) => {
        const isActive = active[stage.key];
        const cssVar = { '--c': `var(--pl-${stage.key})` } as CSSProperties;
        return (
          <div key={stage.key} className="contents">
            <div className={`pl-stage ${isActive ? 'is-active' : counts[stage.key] === 0 ? 'is-idle' : ''}`} style={cssVar}>
              <div className="pl-ring">
                <stage.Icon />
                <span className="pl-badge">{counts[stage.key]}</span>
                {stage.key === 'plan' && (
                  <span className="pl-mcp" title="Fetching context (MCP)"><SignalIcon /></span>
                )}
              </div>
              <span className="pl-label">{stage.label}</span>
              {/* agent terminals under the Code node — one per active agent/subtask */}
              {stage.key === 'code' && (termCount > 0 || overflow > 0) && (
                <div className="pl-agents">
                  <AnimatePresence>
                    {Array.from({ length: termCount }).map((_, idx) => (
                      <motion.span
                        key={idx}
                        className="pl-term"
                        initial={{ opacity: 0, scale: 0.4, y: -6 }}
                        animate={{ opacity: 1, scale: 1, y: 0 }}
                        exit={{ opacity: 0, scale: 0.4, y: 6 }}
                        transition={{ delay: idx * 0.07, type: 'spring', stiffness: 420, damping: 26 }}
                      >
                        <TerminalIcon />
                      </motion.span>
                    ))}
                  </AnimatePresence>
                  {overflow > 0 && <span className="pl-term-more">+{overflow}</span>}
                </div>
              )}
            </div>
            {i < STAGES.length - 1 && (
              <div
                className={`pl-link ${(isActive || active[STAGES[i + 1].key]) ? 'is-flowing' : ''}`}
                style={{ '--c': `var(--pl-${stage.key})` } as CSSProperties}
              />
            )}
          </div>
        );
      })}
    </div>
  );
}
