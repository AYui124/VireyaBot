import type { HTMLAttributes } from "react";

export function Card({ className = "", ...props }: HTMLAttributes<HTMLDivElement>) {
  const classes = `card ${className}`;
  return <section className={classes} {...props} />;
}

export function CardHeader({ className = "", ...props }: HTMLAttributes<HTMLDivElement>) {
  const classes = `card-header ${className}`;
  return <div className={classes} {...props} />;
}

export function CardBody({ className = "", ...props }: HTMLAttributes<HTMLDivElement>) {
  const classes = `card-body ${className}`;
  return <div className={classes} {...props} />;
}
