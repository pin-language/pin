// Run a command and print its peak memory (KB) to stderr. Tiny parent so the fork itself adds ~nothing.
#include <stdio.h>
#include <sys/resource.h>
#include <sys/wait.h>
#include <unistd.h>
#include <fcntl.h>

int main(int argc, char **argv) {
    pid_t pid = fork();
    if (pid == 0) {
        int fd = open("/dev/null", O_WRONLY);
        dup2(fd, 1);
        execvp(argv[1], argv + 1);
        _exit(127);
    }
    int status;
    struct rusage ru;
    wait4(pid, &status, 0, &ru);
    fprintf(stderr, "%ld\n", ru.ru_maxrss);
    return WEXITSTATUS(status);
}
