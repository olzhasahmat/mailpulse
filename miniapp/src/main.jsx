import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App.jsx";
import "./styles.css";

const tg = window.Telegram?.WebApp;
tg?.ready();
tg?.expand();

createRoot(document.getElementById("root")).render(<App />);
