# SudharSarthi (सुधारसारथी)
### Intelligent Civic Defect & Road Safety Intelligence Platform • Duhai, Ghaziabad

[![Deploy with Vercel](https://vercel.com/button)](https://vercel.com/new/clone?repository-url=https%3A%2F%2Fgithub.com%2FA-santusht%2Fsudharsarthi_1&project-name=sudharsarthi)
[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/A-santusht/sudharsarthi_1)

---

## 🔗 How to Connect Vercel & Render (Instant Setup)

1. **Deploy the Backend on Render**:
   - Click the **[Deploy to Render](https://render.com/deploy?repo=https://github.com/A-santusht/sudharsarthi_1)** button.
   - Render automatically reads `/render.yaml` and starts your Gunicorn Python backend for free.
   - Copy the live URL Render gives you (e.g. `https://sudharsarthi.onrender.com` or `https://your-app.onrender.com`).

2. **Open Your Vercel Website**:
   - Open your deployed Vercel site.
   - In the **left sidebar** under **Cloud Backend (Render)**, paste your Render URL into the input box and click **Connect**.
   - The status turns **🟢 Connected to Render** and synchronizes all live incident data across Vercel and Render immediately!

---

## ✨ Features
- **FlowPilot Minimalist SaaS UI**: Clean 3-column layout featuring high-contrast KPI cards, weekly defect load heatmap, active incident timeline cards, and a minimalist map.
- **Stable, Non-Glitchy GIS Map**: Built with clean CartoDB Positron tiles. Map zooms smoothly on button clicks without erratic wheel-zoom jumps; markers only open on explicit user click (no jittery hover popups).
- **Automated Email Dispatch**: Every lodged report is instantly emailed with live coordinates, defect photo, and AI urgency assessment to `awasthishailendra59@gmail.com`.
- **0/1 Knapsack Budget Optimizer**: Allocates municipal funds to maximize road hazard mitigation.
- **CORS Preflight & Reverse Proxy**: Seamless cross-cloud communication between Vercel static frontend and Render Flask backend.
