import { useState } from "react";
import { supabase } from "../lib/supabaseClient";
import { Mail, Lock, Loader2, Chrome, Send, ArrowLeft } from "lucide-react";

interface AuthScreenProps {
  /** Если передан, показывает кнопку "Назад" для возврата на дашборд */
  onCancel?: () => void;
}

export default function AuthScreen({ onCancel }: AuthScreenProps) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const handleSignIn = async () => {
    setError(null);
    setBusy(true);
    try {
      const { error } = await supabase.auth.signInWithPassword({ email: email.trim(), password });
      if (error) setError(error.message);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Sign in failed");
    } finally {
      setBusy(false);
    }
  };

  const handleSignUp = async () => {
    setError(null);
    setBusy(true);
    try {
      const { error } = await supabase.auth.signUp({ email: email.trim(), password });
      if (error) setError(error.message);
      else setError("Check your email to confirm the registration.");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Sign up failed");
    } finally {
      setBusy(false);
    }
  };

  const handleGoogle = async () => {
    setError(null);
    try {
      const { error } = await supabase.auth.signInWithOAuth({ provider: "google" });
      if (error) setError(error.message);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Google sign in failed");
    }
  };

  const handleTelegram = () => {
    // Пока визуальная заглушка
    console.log("Telegram login click");
  };

  return (
    <div className="min-h-screen flex items-center justify-center bg-zinc-950 px-4">
      <div className="w-full max-w-sm">
        <div className="rounded-2xl border border-zinc-800 bg-zinc-900 shadow-xl p-8">
          {onCancel && (
            <button
              onClick={onCancel}
              className="mb-4 inline-flex items-center gap-1.5 text-sm text-zinc-400 hover:text-white transition-colors"
            >
              <ArrowLeft className="w-4 h-4" />
              Back to dashboard
            </button>
          )}
          <div className="mb-6 text-center">
            <h1 className="text-2xl font-bold text-white tracking-tight">
              Trading Bot
            </h1>
            <p className="mt-1 text-sm text-zinc-400">
              Sign in to access your dashboard
            </p>
          </div>

          <div className="space-y-4">
            {/* Email */}
            <div>
              <label className="block text-xs font-medium text-zinc-400 mb-1.5">
                Email
              </label>
              <div className="relative">
                <Mail className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-zinc-500" />
                <input
                  type="email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  placeholder="you@example.com"
                  className="w-full rounded-md bg-zinc-800 border border-zinc-700 pl-9 pr-3 py-2 text-sm text-white placeholder-zinc-500 focus:outline-none focus:ring-1 focus:ring-blue-500 focus:border-blue-500"
                />
              </div>
            </div>

            {/* Password */}
            <div>
              <label className="block text-xs font-medium text-zinc-400 mb-1.5">
                Password
              </label>
              <div className="relative">
                <Lock className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-zinc-500" />
                <input
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="••••••••"
                  onKeyDown={(e) => e.key === "Enter" && handleSignIn()}
                  className="w-full rounded-md bg-zinc-800 border border-zinc-700 pl-9 pr-3 py-2 text-sm text-white placeholder-zinc-500 focus:outline-none focus:ring-1 focus:ring-blue-500 focus:border-blue-500"
                />
              </div>
            </div>

            {/* Error */}
            {error && (
              <div className="rounded-md bg-red-500/10 border border-red-500/30 px-3 py-2 text-xs text-red-400">
                {error}
              </div>
            )}

            {/* Sign In / Sign Up */}
            <div className="grid grid-cols-2 gap-3">
              <button
                onClick={handleSignIn}
                disabled={busy}
                className="inline-flex items-center justify-center rounded-md bg-blue-600 hover:bg-blue-500 disabled:opacity-50 disabled:cursor-not-allowed text-sm font-medium text-white px-4 py-2 transition-colors"
              >
                {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : "Sign In"}
              </button>
              <button
                onClick={handleSignUp}
                disabled={busy}
                className="inline-flex items-center justify-center rounded-md bg-zinc-800 border border-zinc-700 hover:bg-zinc-700 disabled:opacity-50 disabled:cursor-not-allowed text-sm font-medium text-white px-4 py-2 transition-colors"
              >
                {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : "Sign Up"}
              </button>
            </div>

            {/* Divider */}
            <div className="flex items-center gap-3 pt-1">
              <div className="h-px flex-1 bg-zinc-800" />
              <span className="text-xs text-zinc-500">or</span>
              <div className="h-px flex-1 bg-zinc-800" />
            </div>

            {/* Google */}
            <button
              onClick={handleGoogle}
              className="w-full inline-flex items-center justify-center gap-2 rounded-md bg-zinc-800 border border-zinc-700 hover:bg-zinc-700 text-sm font-medium text-white px-4 py-2 transition-colors"
            >
              <Chrome className="w-4 h-4" />
              Continue with Google
            </button>

            {/* Telegram */}
            <button
              onClick={handleTelegram}
              className="w-full inline-flex items-center justify-center gap-2 rounded-md bg-[#229ED9] hover:bg-[#1c8cbf] text-sm font-medium text-white px-4 py-2 transition-colors"
            >
              <Send className="w-4 h-4" />
              Continue with Telegram
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
