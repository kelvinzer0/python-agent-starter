declare module '*.module.css' {
  const classes: { readonly [key: string]: string };
  export default classes;
}

declare module '@phcode/fs/dist/virtualfs.js' {
  const content: any;
  export default content;
}

declare module 'buffer' {
  export const Buffer: any;
}
