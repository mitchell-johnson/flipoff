import { Board } from './Board.js';
import { GRID_COLS, TOTAL_TRANSITION } from './constants.js';

const KIOSK_ROWS = 11;
const DEFAULT_INTERVAL = 15000;

function getParams() {
  const params = new URLSearchParams(window.location.search);
  return {
    data: params.get('data'),
    url: params.get('url'),
    interval: parseInt(params.get('interval')) || null,
  };
}

function generateDefaultContent() {
  const now = new Date();
  const dayName = now.toLocaleDateString('en-NZ', { weekday: 'long' }).toUpperCase();
  const month = now.toLocaleDateString('en-NZ', { month: 'long' }).toUpperCase();
  const day = now.getDate();
  const year = now.getFullYear();

  return {
    pages: [
      {
        lines: [dayName, `${month} ${day}, ${year}`, '', 'AUCKLAND, NZ', '', ''],
      },
    ],
    interval: DEFAULT_INTERVAL,
  };
}

async function loadContent() {
  const params = getParams();

  if (params.data) {
    try {
      const json = JSON.parse(atob(params.data));
      if (params.interval) json.interval = params.interval;
      return json;
    } catch (e) {
      console.error('Failed to parse data param:', e);
    }
  }

  if (params.url) {
    try {
      const resp = await fetch(params.url);
      const json = await resp.json();
      if (params.interval) json.interval = params.interval;
      return json;
    } catch (e) {
      console.error('Failed to fetch from URL:', e);
    }
  }

  const content = generateDefaultContent();
  if (params.interval) content.interval = params.interval;
  return content;
}

document.addEventListener('DOMContentLoaded', async () => {
  const container = document.getElementById('board-container');
  const board = new Board(container, null, {
    rows: KIOSK_ROWS,
    cols: GRID_COLS,
    showChrome: false,
  });

  const content = await loadContent();
  const pages = content.pages || [];
  const interval = content.interval || DEFAULT_INTERVAL;

  let pageIndex = 0;

  function showPage(index) {
    if (pages.length === 0) return;
    pageIndex = ((index % pages.length) + pages.length) % pages.length;
    board.displayMessage(pages[pageIndex].lines);
  }

  function nextPage() {
    if (board.isTransitioning) return;
    showPage(pageIndex + 1);
  }

  // Show first page
  if (pages.length > 0) {
    showPage(0);
  }

  // Auto-rotate if multiple pages
  if (pages.length > 1) {
    setInterval(() => {
      nextPage();
    }, interval);
  }

  // Expose control API for CLI/Playwright
  window.flipframe = {
    nextPage,
    showPage,
    getCurrentPage: () => pageIndex,
    getTotalPages: () => pages.length,
    isTransitioning: () => board.isTransitioning,
    getInterval: () => interval,
  };
});
