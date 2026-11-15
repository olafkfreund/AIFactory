import type {ReactNode} from 'react';
import Link from '@docusaurus/Link';
import useBaseUrl from '@docusaurus/useBaseUrl';
import useDocusaurusContext from '@docusaurus/useDocusaurusContext';
import Layout from '@theme/Layout';
import Heading from '@theme/Heading';

function HeroBanner() {
  const {siteConfig} = useDocusaurusContext();
  // Resolve against the site baseUrl so the logo loads regardless of the
  // deploy path (a bare "img/..." is relative to the current URL and 404s).
  const logoUrl = useBaseUrl('/img/aifactory-logo.png');
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
          src={logoUrl}
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
    title: 'Runs in your perimeter',
    body:
      'Self-hosted on your own Kubernetes via the Helm chart (or docker-compose on a laptop). ' +
      'Your source never has to leave your network — the answer for teams that legally can\'t ' +
      'ship code to a third-party cloud.',
  },
  {
    title: 'Spec-first, review-gated',
    body:
      'Every run starts from a written spec with acceptance criteria. You approve the plan ' +
      'before code is written and the diff before it merges; a QA agent checks the work. ' +
      'No unsupervised black box, no vibe-code to clean up.',
  },
  {
    title: 'Auditable by design',
    body:
      'Every action is journaled in a hash-chained audit log; every spec, plan, and QA report ' +
      'lives on disk and in version control. The provenance trail your security team and your ' +
      'auditor actually ask for — SOC2 / ISO evidence in the enterprise build.',
  },
  {
    title: 'No vendor lock-in',
    body:
      'Route each phase to the right model — Claude, OpenAI, Gemini, Codex, or a local Ollama / ' +
      'OpenAI-compatible endpoint. Provider abstraction means you own your model bill and never ' +
      'depend on a single vendor.',
  },
  {
    title: 'Isolated + observable',
    body:
      'Each task runs in its own git worktree — nothing touches your tree until you merge. ' +
      'Watch the agent work in real time via the Live Console (read-only by default, one-click ' +
      'Attach), with OpenTelemetry tracing across every boundary.',
  },
  {
    title: 'Merge gated on evidence',
    body:
      'Auto-merge needs a signed contract, a TFactory verdict and green CI on the exact ' +
      'head commit, fetched by AIFactory itself. Tasks with a production target or a high ' +
      'risk class hold for a human. Projects without TFactory or CI no longer auto-merge.',
  },
  {
    title: 'The coding agent is untrusted',
    body:
      'Agent environments are scrubbed of secrets, each spawn gets a private HOME and an ' +
      'allowlist, server-side git is pinned against agent-written config, and run gates ' +
      'are read once before the first agent session. Some gaps remain and are listed in the release notes.',
  },
  {
    title: 'MCP control plane',
    body:
      'Drive AIFactory from Claude Code, Cursor, Continue.dev, or any MCP-aware client over ' +
      'stdio or HTTP+SSE, with scoped API keys, and hand a task off to an autonomous ' +
      'background run with the /handover skill. Remote MCP is off by default.',
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
    when: 'October 2026',
    title: 'Merge gated on a TFactory verdict and green CI',
    body:
      'v3.10.0 auto-merges a verified task only on a TFactory verdict plus green host CI ' +
      'for the exact head commit. Hosts without TFactory or CI hold for a human after 20 minutes.',
    link: '/blog/what-shipped-august-to-november',
    linkText: 'Read the summary →',
  },
  {
    when: 'October 2026',
    title: 'Agent environments scrubbed',
    body:
      'Non-Claude agent CLIs (Codex, Gemini, Copilot, OpenCode) and the Ollama/OpenAI-compatible ' +
      'loops start with scrubbed environments (v3.10.0). v3.10.3 adds a private HOME and an ' +
      'allowlist; operators add names through AIFACTORY_AGENT_ENV_EXTRA.',
    link: '/blog/what-shipped-august-to-november',
    linkText: 'Read the summary →',
  },
  {
    when: 'October 2026',
    title: 'Scoped API keys that work, and enforced OIDC claims',
    body:
      'acw_ keys authenticate (before, every real key failed), remote MCP checks each tool ' +
      'call and stays off by default, and OIDC issuer, audience and expiry are enforced.',
    link: '/concepts/mcp-stdio-keys',
    linkText: 'Scoped MCP keys →',
  },
  {
    when: 'August 2026',
    title: 'Outbound PII scrubbing on by default',
    body:
      'v3.6.75 scrubs outbound model calls by default and fails closed. Projects classed ' +
      'regulated (v3.10.8, with direct claude --print spawns in v3.10.9) always scrub.',
    link: '/concepts/litellm-gateway',
    linkText: 'Gateway and scrubbing (LiteLLM sub-chart deprecated, still works) →',
  },
  {
    when: 'September 2026',
    title: 'Safer Kubernetes build Jobs',
    body:
      'v3.7.0 gives build Jobs an ephemeral-storage limit and stops a crashed dispatch ' +
      'from leaking a concurrency slot. v3.10.15 caps Postgres pools at 2+2 per pod.',
    link: '/blog/what-shipped-august-to-november',
    linkText: 'Read the summary →',
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
          <Link href="https://cfactory.freundcloud.com/">CFactory</Link>{' '}
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
