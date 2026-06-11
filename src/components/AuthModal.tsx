import { useState } from 'react';
import { loginUser, registerUser, type AuthUser } from '../api';

interface AuthModalProps {
  onAuth: (user: AuthUser) => void;
}

export default function AuthModal({ onAuth }: AuthModalProps) {
  const [mode, setMode] = useState<'login' | 'register'>('login');
  const [email, setEmail] = useState('');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    setLoading(true);

    try {
      let result;
      if (mode === 'register') {
        result = await registerUser(email, username, password);
      } else {
        result = await loginUser(email, password);
      }

      if ('error' in result) {
        setError(result.error);
      } else {
        onAuth(result);
      }
    } catch {
      setError('An unexpected error occurred');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div style={{
      position: 'fixed',
      inset: 0,
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
      backgroundColor: 'rgba(0, 0, 0, 0.5)',
      zIndex: 1000,
    }}>
      <div style={{
        background: 'var(--bg-primary, #fff)',
        borderRadius: '12px',
        padding: '32px',
        width: '100%',
        maxWidth: '400px',
        boxShadow: '0 20px 60px rgba(0, 0, 0, 0.3)',
      }}>
        <h2 style={{
          margin: '0 0 8px',
          fontSize: '24px',
          fontWeight: 600,
          color: 'var(--text-primary, #1a1a1a)',
          textAlign: 'center',
        }}>
          {mode === 'login' ? 'Welcome Back' : 'Create Account'}
        </h2>
        <p style={{
          margin: '0 0 24px',
          fontSize: '14px',
          color: 'var(--text-secondary, #666)',
          textAlign: 'center',
        }}>
          {mode === 'login'
            ? 'Sign in to access your workspace'
            : 'Register to start using AI Studio'}
        </p>

        <form onSubmit={handleSubmit}>
          <div style={{ marginBottom: '16px' }}>
            <label style={{
              display: 'block',
              marginBottom: '6px',
              fontSize: '13px',
              fontWeight: 500,
              color: 'var(--text-primary, #1a1a1a)',
            }}>
              Email
            </label>
            <input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
              style={{
                width: '100%',
                padding: '10px 12px',
                fontSize: '14px',
                border: '1px solid var(--border-color, #e0e0e0)',
                borderRadius: '8px',
                backgroundColor: 'var(--bg-secondary, #f5f5f5)',
                color: 'var(--text-primary, #1a1a1a)',
                outline: 'none',
                boxSizing: 'border-box',
              }}
              placeholder="you@example.com"
            />
          </div>

          {mode === 'register' && (
            <div style={{ marginBottom: '16px' }}>
              <label style={{
                display: 'block',
                marginBottom: '6px',
                fontSize: '13px',
                fontWeight: 500,
                color: 'var(--text-primary, #1a1a1a)',
              }}>
                Username
              </label>
              <input
                type="text"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                required
                style={{
                  width: '100%',
                  padding: '10px 12px',
                  fontSize: '14px',
                  border: '1px solid var(--border-color, #e0e0e0)',
                  borderRadius: '8px',
                  backgroundColor: 'var(--bg-secondary, #f5f5f5)',
                  color: 'var(--text-primary, #1a1a1a)',
                  outline: 'none',
                  boxSizing: 'border-box',
                }}
                placeholder="Your name"
              />
            </div>
          )}

          <div style={{ marginBottom: '16px' }}>
            <label style={{
              display: 'block',
              marginBottom: '6px',
              fontSize: '13px',
              fontWeight: 500,
              color: 'var(--text-primary, #1a1a1a)',
            }}>
              Password
            </label>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
              minLength={6}
              style={{
                width: '100%',
                padding: '10px 12px',
                fontSize: '14px',
                border: '1px solid var(--border-color, #e0e0e0)',
                borderRadius: '8px',
                backgroundColor: 'var(--bg-secondary, #f5f5f5)',
                color: 'var(--text-primary, #1a1a1a)',
                outline: 'none',
                boxSizing: 'border-box',
              }}
              placeholder="Min 6 characters"
            />
          </div>

          {error && (
            <div style={{
              marginBottom: '16px',
              padding: '10px 12px',
              fontSize: '13px',
              color: '#d32f2f',
              backgroundColor: '#fdecea',
              borderRadius: '8px',
            }}>
              {error}
            </div>
          )}

          <button
            type="submit"
            disabled={loading}
            style={{
              width: '100%',
              padding: '12px',
              fontSize: '14px',
              fontWeight: 600,
              color: '#fff',
              backgroundColor: loading ? '#999' : '#1a1a1a',
              border: 'none',
              borderRadius: '8px',
              cursor: loading ? 'not-allowed' : 'pointer',
              marginBottom: '16px',
            }}
          >
            {loading ? 'Please wait...' : mode === 'login' ? 'Sign In' : 'Create Account'}
          </button>
        </form>

        <div style={{ textAlign: 'center' }}>
          <button
            onClick={() => {
              setMode(mode === 'login' ? 'register' : 'login');
              setError('');
            }}
            style={{
              background: 'none',
              border: 'none',
              fontSize: '13px',
              color: 'var(--text-secondary, #666)',
              cursor: 'pointer',
              textDecoration: 'underline',
            }}
          >
            {mode === 'login'
              ? "Don't have an account? Register"
              : 'Already have an account? Sign in'}
          </button>
        </div>
      </div>
    </div>
  );
}
