/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        stone: { 50: '#f7f9fa', 100: '#edf1f3', 200: '#dfe6ea', 300: '#c9d5db', 400: '#8fa2ad', 500: '#71868f', 600: '#506873', 700: '#354d58', 800: '#293d47', 900: '#172a34' },
        sky: { 50: '#edf8f3', 100: '#d4eee4', 200: '#b6dfcd', 300: '#8fcdb7', 400: '#55bba0', 500: '#249c85', 600: '#138977', 700: '#087f75', 800: '#096256', 900: '#144a40' },
      },
      fontFamily: {
        sans: ['Pretendard', 'system-ui', '-apple-system', 'Segoe UI', 'sans-serif'],
        mono: ['JetBrains Mono', 'ui-monospace', 'Menlo', 'monospace'],
      },
    },
  },
  plugins: [],
}
