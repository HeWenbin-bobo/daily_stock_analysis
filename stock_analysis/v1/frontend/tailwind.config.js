/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        up: '#ef4444',     // A股惯例: 红涨
        down: '#10b981',   // 绿跌
        bg: '#0f172a',
        panel: '#1e293b',
        border: '#334155',
      },
    },
  },
  plugins: [],
};