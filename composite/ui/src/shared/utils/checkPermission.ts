type RbacCheck = {
    can: (action: string) => boolean;
    canAny: (actions: readonly string[]) => boolean;
    canAll: (actions: readonly string[]) => boolean;
};

type RbacProps = {
    action?: string;
    anyOf?: readonly string[];
    allOf?: readonly string[];
};

export function checkPermission(
    { action, anyOf, allOf }: RbacProps,
    { can, canAny, canAll }: RbacCheck,
): boolean {
    if (action) return can(action);
    if (anyOf?.length) return canAny(anyOf);
    if (allOf?.length) return canAll(allOf);
    return true;
}
