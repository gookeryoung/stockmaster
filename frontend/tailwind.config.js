/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Chinese market convention: red = up, green = down.
        up: "#ef4444",
        down: "#22c55e",
        flat: "#94a3b8",
      },
    },
  },
  plugins: [],
};
