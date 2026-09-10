import { Navigate, Outlet, Route, Routes } from 'react-router-dom';
import AppShell from './components/layout/AppShell.jsx';
import LoginPage from './pages/LoginPage.jsx';
import SandboxDashboardPage from './pages/SandboxDashboardPage.jsx';
import PipelineMetricsPage from './pages/PipelineMetricsPage.jsx';
import MeetingOutcomesPage from './pages/MeetingOutcomesPage.jsx';
import OVSLandingPage from './pages/OVSLandingPage.jsx';
import { auth } from './api/client.js';

function RequireAuth() {
  return auth.getToken() ? <Outlet /> : <Navigate to="/login" replace />;
}

export default function App() {
  return (
    <Routes>
      {/* Public */}
      <Route path="/login" element={<LoginPage />} />
      <Route path="/audit" element={<OVSLandingPage />} />

      {/* Internal dashboard — requires JWT */}
      <Route element={<RequireAuth />}>
        <Route path="/" element={<AppShell />}>
          <Route index element={<Navigate to="/sandbox" replace />} />
          <Route path="sandbox" element={<SandboxDashboardPage />} />
          <Route path="metrics" element={<PipelineMetricsPage />} />
          <Route path="meetings" element={<MeetingOutcomesPage />} />
        </Route>
      </Route>
    </Routes>
  );
}
