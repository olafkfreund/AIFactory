import { useState } from 'react';
import { Button } from '../components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '../components/ui/dialog';
import { Input } from '../components/ui/input';
import type { Project } from '../shared/types';
import { removeProject } from '../stores/project-store';

export function ProjectsPage({
  projects,
  slugs,
  onAdd,
  onOpen,
}: {
  projects: Project[];
  slugs: Map<string, string>;
  onAdd: () => void;
  onOpen: (p: Project) => void;
}) {
  const [deleting, setDeleting] = useState<Project | null>(null);
  const [typedName, setTypedName] = useState('');

  return (
    <main className="mx-auto max-w-5xl space-y-6 p-6 text-foreground">
      <div className="flex items-center justify-between gap-4">
        <h1 className="text-2xl font-semibold">Projects</h1>
        <Button onClick={onAdd}>Add project</Button>
      </div>

      {projects.length === 0 ? (
        <p className="text-muted-foreground">No projects yet.</p>
      ) : (
        <div className="divide-y divide-border rounded-lg border border-border bg-card">
          {projects.map((project) => (
            <div key={project.id} className="flex items-center gap-4 p-4">
              <div className="min-w-0 flex-1">
                <div className="font-medium">{project.name}</div>
                <div className="truncate text-sm text-muted-foreground" title={project.path}>
                  {project.path}
                </div>
                <div className="font-mono text-xs text-muted-foreground">
                  {slugs.get(project.id)}
                </div>
              </div>
              <Button variant="outline" onClick={() => onOpen(project)}>Open</Button>
              <Button variant="ghost" className="text-destructive" onClick={() => setDeleting(project)}>
                Delete
              </Button>
            </div>
          ))}
        </div>
      )}

      <Dialog open={deleting !== null} onOpenChange={(open) => {
        if (!open) {
          setDeleting(null);
          setTypedName('');
        }
      }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Unregister project</DialogTitle>
            <DialogDescription>
              This only unregisters the project from AIFactory. Its files stay untouched.
              Type <strong>{deleting?.name}</strong> to confirm.
            </DialogDescription>
          </DialogHeader>
          <label className="space-y-2 text-sm">
            <span>Project name</span>
            <Input value={typedName} onChange={(event) => setTypedName(event.target.value)} />
          </label>
          <DialogFooter>
            <Button variant="outline" onClick={() => {
              setDeleting(null);
              setTypedName('');
            }}>Cancel</Button>
            <Button
              variant="destructive"
              disabled={typedName !== deleting?.name}
              onClick={() => {
                if (!deleting || typedName !== deleting.name) return;
                void removeProject(deleting.id);
                setDeleting(null);
                setTypedName('');
              }}
            >
              Unregister project
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </main>
  );
}
