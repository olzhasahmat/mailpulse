import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Прокси на API в dev: фронт на 5173, бэкенд на 8000
export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": "http://localhost:8000" } },
});
