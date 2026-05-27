import type {SidebarsConfig} from '@docusaurus/plugin-content-docs';

/**
 * Two manual sidebars + (later) an OpenAPI-generated one.
 *
 *   mainSidebar — the primary user journey
 *   wikiSidebar — reference-style FAQ / Troubleshooting / Glossary
 *   apiSidebar  — added in Phase B2 by the openapi-docs plugin
 */

const sidebars: SidebarsConfig = {
  mainSidebar: [
    'intro',
    'getting-started',
    'demo',
    {
      type: 'category',
      label: 'Concepts',
      collapsed: false,
      items: [
        'concepts/spec-driven-development',
        'concepts/multi-provider',
        'concepts/rmux-live-console',
        'concepts/remote-control',
        'concepts/delegation',
        'concepts/mcp-servers',
        'concepts/mcp-credentials',
        'concepts/mcp-stdio-keys',
      ],
    },
    {
      type: 'category',
      label: 'Architecture',
      collapsed: false,
      items: [
        'architecture/overview',
        'architecture/agents',
        'architecture/data-flow',
      ],
    },
    {
      type: 'category',
      label: 'Showcase',
      collapsed: true,
      items: ['showcase/index', 'showcase/demo-walkthrough', 'showcase/enterprise-demo'],
    },
    {
      type: 'category',
      label: 'Compliance',
      collapsed: true,
      items: ['compliance/soc2', 'compliance/gdpr'],
    },
    'contributing',
    'roadmap',
  ],

  wikiSidebar: [
    {
      type: 'category',
      label: 'Wiki',
      collapsed: false,
      items: ['wiki/faq', 'wiki/troubleshooting', 'wiki/glossary'],
    },
  ],
};

export default sidebars;
