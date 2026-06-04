import type {ReactNode} from 'react';
import Link from '@docusaurus/Link';
import useDocusaurusContext from '@docusaurus/useDocusaurusContext';
import Layout from '@theme/Layout';
import Heading from '@theme/Heading';

function HeroBanner() {
  const {siteConfig} = useDocusaurusContext();
  return (
    <header className="hero-banner">
      {/* Animated gradient orbs in the background — pure CSS, no JS */}
      <div className="hero-orbs" aria-hidden="true">
        <span className="orb orb-1" />
        <span className="orb orb-2" />
        <span className="orb orb-3" />
      </div>
      <div className="container hero-content">
        <img
          src="img/aifactory-logo.png"
          alt="AIFactory"
          className="hero-logo"
        />
        <Heading as="h1" className="hero-title">{siteConfig.title}</Heading>
        <p className="tagline">{siteConfig.tagline}</p>
        <div className="hero-meta">
          <span className="hero-badge">Self-hosted</span>
          <span className="hero-badge">Review-gated</span>
          <span className="hero-badge">Fully auditable</span>
          <span className="hero-badge">No lock-in</span>
        </div>
        <div className="hero-cta">
          <Link
            className="button button--secondary button--lg"
            to="/getting-started">
            Get started →
          </Link>
          <Link
            className="button button--outline button--lg hero-cta-secondary"
            to="/demo">
            Watch the demo
          </Link>
        </div>
      </div>
    </header>
  );
}

// ─── Feature cards — three pillars of the platform ──────────────────────

const FEATURES = [
  {
    icon: '🔒',
    title: 'Runs in your perimeter',
    body:
      'Self-hosted on your own Kubernetes via the Helm chart (or docker-compose on a laptop). ' +
      'Your source never has to leave your network — the answer for teams that legally can\'t ' +
      'ship code to a third-party cloud.',
  },
  {
    icon: '📝',
    title: 'Spec-first, review-gated',
    body:
      'Every run starts from a written spec with acceptance criteria. You approve the plan ' +
      'before code is written and the diff before it merges; a QA agent checks the work. ' +
      'No unsupervised black box, no vibe-code to clean up.',
  },
  {
    icon: '🧾',
    title: 'Auditable by design',
    body:
      'Every action is journaled in a hash-chained audit log; every spec, plan, and QA report ' +
      'lives on disk and in version control. The provenance trail your security team and your ' +
      'auditor actually ask for — SOC2 / ISO evidence in the enterprise build.',
  },
  {
    icon: '🔀',
    title: 'No vendor lock-in',
    body:
      'Route each phase to the right model — Claude, OpenAI, Gemini, Codex, or a local Ollama / ' +
      'OpenAI-compatible endpoint. Provider abstraction means you own your model bill and never ' +
      'depend on a single vendor.',
  },
  {
    icon: '🖥️',
    title: 'Isolated + observable',
    body:
      'Each task runs in its own git worktree — nothing touches your tree until you merge. ' +
      'Watch the agent work in real time via the Live Console (read-only by default, one-click ' +
      'Attach), with OpenTelemetry tracing across every boundary.',
  },
  {
    icon: '🛰️',
    title: 'MCP control plane',
    body:
      '27 MCP tools across stdio + HTTP+SSE transports. Drive AIFactory from Claude Code, ' +
      'Cursor, Continue.dev, or any MCP-aware client — and hand a task off to an autonomous ' +
      'background run with the /handover skill.',
  },
];

function FeatureGrid() {
  return (
    <section className="features-section">
      <div className="features-header">
        <Heading as="h2">Autonomy you can actually defend</Heading>
        <p className="features-subtitle">
          For teams that can't send their code to the cloud and won't merge what they can't review.
          Your infra, your model bill, your audit trail.
        </p>
      </div>
      <div className="features">
        {FEATURES.map((f, i) => (
          <div
            key={f.title}
            className="feature-card reveal-on-scroll"
            style={{animationDelay: `${i * 80}ms`}}>
            <div className="feature-icon" aria-hidden="true">{f.icon}</div>
            <Heading as="h3">{f.title}</Heading>
            <p>{f.body}</p>
          </div>
        ))}
      </div>
    </section>
  );
}

// ─── Recently shipped — auto-running "what's new" timeline ──────────────

const RECENT = [
  {
    when: 'May 2026',
    title: 'MCP Control-Plane Epic shipped',
    body:
      '27 task-control tools across stdio + remote HTTP+SSE transports. Drive AIFactory ' +
      'from Claude Code, Cursor, or any MCP-aware client.',
    link: '/concepts/multi-provider',
    linkText: 'Read the design →',
  },
  {
    when: 'May 2026',
    title: '/handover skill — async hand-off from any chat',
    body:
      'Type /handover in Claude Code. AIFactory summarises the conversation, creates ' +
      'a spec, and runs the build while you do something else. Comes back as a draft PR.',
    link: '/concepts/multi-provider',
    linkText: 'See the workflow →',
  },
  {
    when: 'May 2026',
    title: 'Default MCP servers — Kubernetes / AWS / Azure / GitHub',
    body:
      'A catalog of well-maintained MCP servers auto-enables per project when infra ' +
      'markers + credentials line up. Read-only by default. CVE-aware version pins.',
    link: '/concepts/multi-provider',
    linkText: 'Setup guide →',
  },
  {
    when: 'May 2026',
    title: 'Remote Control — drive AIFactory tasks from your phone',
    body:
      'Wires Claude Code\'s native --remote-control flag into the agent spawn. Open ' +
      'claude.ai/code on any device and pick up where you left off.',
    link: '/concepts/remote-control',
    linkText: 'Operator guide →',
  },
];

function RecentlyShipped() {
  return (
    <section className="recent-section">
      <div className="recent-header">
        <Heading as="h2">Recently shipped</Heading>
        <p className="features-subtitle">
          The platform moves fast. Here's what landed lately.
        </p>
      </div>
      <div className="recent-timeline">
        {RECENT.map((r, i) => (
          <article
            key={r.title}
            className="recent-item reveal-on-scroll"
            style={{animationDelay: `${i * 100}ms`}}>
            <span className="recent-when">{r.when}</span>
            <Heading as="h3" className="recent-title">{r.title}</Heading>
            <p>{r.body}</p>
            <Link to={r.link} className="recent-link">{r.linkText}</Link>
          </article>
        ))}
      </div>
    </section>
  );
}

// ─── CTA strip at the bottom ───────────────────────────────────────────

function CTAStrip() {
  return (
    <section className="cta-strip">
      <div className="container">
        <Heading as="h2">Ready to try it?</Heading>
        <p>
          Self-hosted, MIT/GPL-3.0 dual-licensed. Bring your own Claude OAuth seat
          (or run it against Ollama). Zero lock-in.
        </p>
        <div className="hero-cta">
          <Link
            className="button button--secondary button--lg"
            to="/getting-started">
            Get started in 5 minutes
          </Link>
          <Link
            className="button button--outline button--lg hero-cta-secondary"
            href="https://github.com/olafkfreund/AIFactory"
            target="_blank">
            Star on GitHub ★
          </Link>
        </div>
      </div>
    </section>
  );
}

function FactoryFamily(): ReactNode {
  return (
    <section className="features-section">
      <div
        className="container"
        style={{textAlign: 'center', maxWidth: 820, margin: '0 auto'}}>
        <Heading as="h2">Part of the Factory family</Heading>
        <p>
          AIFactory is the <strong>Act</strong> stage of a governed, verified,
          observable autonomous software factory:{' '}
          <Link href="https://pfactory.freundcloud.com/">PFactory</Link> plans ·{' '}
          <strong>AIFactory</strong> builds ·{' '}
          <Link href="https://tfactory.freundcloud.com/">TFactory</Link> verifies ·{' '}
          <Link href="https://github.com/olafkfreund/CFactory">CFactory</Link>{' '}
          watches over all four.
        </p>
        <div className="hero-cta">
          <Link
            className="button button--secondary button--lg"
            href="https://factory.freundcloud.com/why/">
            Why Factory →
          </Link>
          <Link
            className="button button--outline button--lg hero-cta-secondary"
            href="https://factory.freundcloud.com/">
            The Factory family
          </Link>
        </div>
      </div>
    </section>
  );
}

export default function Home(): ReactNode {
  return (
    <Layout
      title="AIFactory — the open-source AI software engineer you can self-host and audit"
      description="Open-source, self-hostable autonomous coding platform. Spec-first, review-gated, fully auditable, multi-model. Runs in your own cluster — your code never leaves your perimeter.">
      <HeroBanner />
      <main>
        <FeatureGrid />
        <RecentlyShipped />
        <FactoryFamily />
        <CTAStrip />
      </main>
    </Layout>
  );
}
