import { Navigate, Route, Routes } from "react-router-dom";
import { AnimatePresence, MotionConfig, motion } from "motion/react";
import { useLocation } from "react-router-dom";
import { useAuth } from "./auth";
import Layout from "./components/Layout";
import { LoadingState } from "./components/ui";
import Login from "./pages/Login";
import Dashboard from "./pages/Dashboard";
import Upload from "./pages/Upload";
import Search from "./pages/Search";
import Ask from "./pages/Ask";
import Repository from "./pages/Repository";
import DocumentDetail from "./pages/DocumentDetail";
import Duplicates from "./pages/Duplicates";
import Audit from "./pages/Audit";
import Admin from "./pages/Admin";

export default function App() {
  const { user, loading } = useAuth();
  const location = useLocation();

  return (
    <MotionConfig
      reducedMotion="user"
      transition={{ type: "spring", stiffness: 420, damping: 36 }}
    >
      {loading ? (
        <div className="app-loading">
          <LoadingState />
        </div>
      ) : !user ? (
        <Login />
      ) : (
        <Layout>
          <AnimatePresence mode="wait" initial={false}>
            <motion.div
              className="route-stage"
              key={location.pathname}
              initial={{ opacity: 0, x: 12 }}
              animate={{ opacity: 1, x: 0 }}
              exit={{ opacity: 0, x: -8 }}
              transition={{
                opacity: { duration: 0.16, ease: "easeOut" },
                x: { type: "spring", stiffness: 440, damping: 38 },
              }}
            >
              <Routes location={location}>
                <Route path="/" element={<Dashboard />} />
                <Route path="/repository" element={<Repository />} />
                <Route path="/upload" element={<Upload />} />
                <Route path="/search" element={<Search />} />
                <Route path="/ask" element={<Ask />} />
                <Route path="/documents/:id" element={<DocumentDetail />} />
                <Route path="/duplicates" element={<Duplicates />} />
                <Route path="/audit" element={<Audit />} />
                <Route path="/admin" element={<Admin />} />
                <Route path="*" element={<Navigate to="/" replace />} />
              </Routes>
            </motion.div>
          </AnimatePresence>
        </Layout>
      )}
    </MotionConfig>
  );
}
