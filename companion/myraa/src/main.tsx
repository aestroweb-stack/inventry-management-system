import {StrictMode} from 'react';
import {createRoot} from 'react-dom/client';
import App from './App.tsx';
import {ApiKeyGate} from './components/ApiKeyGate.tsx';
import {InventoryAssistantEmbed} from './components/InventoryAssistantEmbed.tsx';
import './index.css';

const isInventoryEmbed = new URLSearchParams(window.location.search).get('embed') === 'inventory';
const appSurface = isInventoryEmbed
  ? <InventoryAssistantEmbed />
  : <ApiKeyGate><App /></ApiKeyGate>;

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    {appSurface}
  </StrictMode>,
);
