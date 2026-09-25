if __name__ == '__main__':
    import sys
    if '--collector' in sys.argv:
        sys.argv.remove('--collector')
        from robust_service import main
    else:
        from client_backend.service import main
    main()
