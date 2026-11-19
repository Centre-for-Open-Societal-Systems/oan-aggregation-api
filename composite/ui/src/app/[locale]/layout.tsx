import type { Metadata } from "next";
import { Roboto } from "next/font/google";
import { NextIntlClientProvider } from "next-intl";
import { getMessages } from "next-intl/server";
import { ToastContainer } from "react-toastify";
import "react-toastify/dist/ReactToastify.css";
import { AuthProviders } from "@/context/AuthProviders";
import Layout from "@/components/Layout";
import "../globals.css";

const roboto = Roboto({
  weight: ["300", "400", "500", "700"],
  style: ["normal"],
  subsets: ["latin"],
  display: "swap",
});

export const metadata: Metadata = {
  title: "Agri Stack Composite",
  description: "OpenG2P Agri Stack use-case composite console",
  icons: {
    icon: "/openg2p-icon.svg",
  },
};

export default async function LocaleLayout({
  children,
  params,
}: Readonly<{
  children: React.ReactNode;
  params: Promise<{ locale: string }>;
}>) {
  const { locale } = await params;
  const messages = await getMessages();

  return (
    <html lang={locale}>
      <body className={`${roboto.className} antialiased`}>
        <NextIntlClientProvider messages={messages}>
          <AuthProviders>
            <Layout>{children}</Layout>
            <ToastContainer position="top-right" autoClose={5000} newestOnTop closeOnClick pauseOnFocusLoss draggable pauseOnHover />
          </AuthProviders>
        </NextIntlClientProvider>
      </body>
    </html>
  );
}
