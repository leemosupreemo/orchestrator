import { initializeApp } from 'firebase/app';
import { getAuth, GoogleAuthProvider, signInWithPopup, signOut } from 'firebase/auth';
import { getFirestore, collection, doc, onSnapshot, query, orderBy, limit, setDoc, updateDoc, serverTimestamp } from 'firebase/firestore';

const app = initializeApp({
  apiKey: import.meta.env.VITE_FIREBASE_API_KEY || 'AIzaSyBg8h8yiC8OCoezFLEq6mQLhlc260b8CcI',
  authDomain: import.meta.env.VITE_FIREBASE_AUTH_DOMAIN || 'swift-orch-web-20260923.firebaseapp.com',
  projectId: import.meta.env.VITE_FIREBASE_PROJECT_ID || 'swift-orch-web-20260923',
  appId: import.meta.env.VITE_FIREBASE_APP_ID || '1:1093947686212:web:95cd975ed0d626ec196b11'
});
export const auth = getAuth(app);
export const db = getFirestore(app);
export const signIn = () => signInWithPopup(auth, new GoogleAuthProvider());
export const logOut = () => signOut(auth);
export { collection, doc, onSnapshot, query, orderBy, limit, setDoc, updateDoc, serverTimestamp };
