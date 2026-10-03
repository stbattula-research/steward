import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import App from './App.jsx';
import './styles.css';

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <App />
  </StrictMode>,
);

// Phone app: the service worker shows notifications (and Approve / Deny on Android).
// Not needed inside the Mac app.
if ('serviceWorker' in navigator && !window.webkit?.messageHandlers?.steward) {
  navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(() => { /* not a secure context */ });
}
