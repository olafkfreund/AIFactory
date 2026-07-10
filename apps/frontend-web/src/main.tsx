import React from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { MotionConfig } from 'motion/react';
import App from './App';
// Distinctive self-hosted type — warm humanist UI sans + a terminal/retro mono
import '@fontsource/hanken-grotesk/400.css';
import '@fontsource/hanken-grotesk/500.css';
import '@fontsource/hanken-grotesk/600.css';
import '@fontsource/hanken-grotesk/700.css';
import '@fontsource/jetbrains-mono/500.css';
import '@fontsource/jetbrains-mono/700.css';
import './index.css';
import './shared/i18n';
import { initWebAPI } from './lib/api-adapter';
import { initializeGitHubListeners } from './stores/github';

// Initialize web API adapter (replaces window.API)
initWebAPI();

// Initialize global GitHub event listeners (PR review progress/complete/error)
// Must be called after initWebAPI() so window.API is available
initializeGitHubListeners();

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <BrowserRouter>
      {/* Respect prefers-reduced-motion for every Framer Motion animation
          (pipeline rings, flying package-box, card springs) — the CSS keyframes
          were already gated, this closes the JS-motion gap. */}
      <MotionConfig reducedMotion="user">
        <App />
      </MotionConfig>
    </BrowserRouter>
  </React.StrictMode>
);
