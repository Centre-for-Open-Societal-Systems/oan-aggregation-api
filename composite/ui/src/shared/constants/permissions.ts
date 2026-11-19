/** IAM permissions of the `agri-composite` application. */
export const PERMISSIONS = {
    view: "composite:view",
    /** Reserved for editing; the console is read-only today. */
    manage: "composite:manage",
} as const;
