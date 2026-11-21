// AeroLink web map. Placeholder until M6 (E6.6): MapLibre GL JS on the shared basemap.
import "./style.css";

const app = document.querySelector<HTMLDivElement>("#app")!;
app.innerHTML = `
  <main>
    <h1>AeroLink</h1>
    <p>Navigation for Florida's low-altitude air corridors, with AI traffic prediction.</p>
    <p class="muted">Web map arrives in M6.</p>
  </main>
`;
