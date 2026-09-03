import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import './index.css';
import App from './App.tsx';
import { SerenaProvider } from './store';

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <SerenaProvider>
      <App />
    </SerenaProvider>
  </StrictMode>,
);
