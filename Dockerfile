FROM eclipse-temurin:8-jre-jammy

RUN mkdir -p /csf
COPY csf /csf

WORKDIR /csf

RUN addgroup --system spring && adduser --system spring --ingroup spring \
    && chown -R spring:spring /csf

USER spring

EXPOSE 8443

ENTRYPOINT ["java", "-jar", "/csf/bin/CSF-3.1.0.jar"]


#docker run -it -v /Users/tezcan/projects/docker/codesearchfinder/dist/config:/csf/config -p 8443:8443 csf-app:v1

#docker run -it -v /Users/tezcan/projects/docker/codesearchfinder/dist/config:/csf/config -v /Users/tezcan/projects/docker/codesearchfinder/dist/projects:/csf/projects -v /Users/tezcan/projects/docker/codesearchfinder/dist/log:/csf/log -p 443:8443 csf-app:v1
